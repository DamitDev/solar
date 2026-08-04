#!/usr/bin/env python3
"""
D-003: ORAS Python Library Evaluation POC

Validates oras-py (v0.2.42) for OCI artifact push/pull against Harbor.

Tests:
    auth            - Authenticate to Harbor, verify credentials via Harbor API
    push_multifile  - Push a multi-file model directory (HuggingFace format)
    pull_verify     - Pull back and verify SHA-256 checksums
    push_single     - Push a single 10MB file (simulating GGUF)
    head_request    - OCI + Harbor API HEAD to check artifact existence
    custom_media    - Push with custom SuperNova media types
    all             - Run all tests (default)

Usage:
    conda run -n data-repository python poc/oras_poc.py
    conda run -n data-repository python poc/oras_poc.py --test auth
    conda run -n data-repository python poc/oras_poc.py --debug
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from base64 import b64encode
from pathlib import Path

import oras.client
import oras.defaults
import oras.oci
import oras.provider
import oras.utils
import requests
import yaml

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
CREDENTIALS_PATH = REPO_ROOT / "temp" / "harbor-credentials.yaml"

HARBOR_HOST = "imgrepo.damit.hu"
HARBOR_PROJECT = "supernova"

TEST_MULTIFILE_REF = f"{HARBOR_HOST}/{HARBOR_PROJECT}/test-model:v1"
TEST_SINGLE_REF = f"{HARBOR_HOST}/{HARBOR_PROJECT}/test-single:v1"
TEST_CUSTOM_MT_REF = f"{HARBOR_HOST}/{HARBOR_PROJECT}/test-custom-media:v1"

MEDIA_TYPES = {
    "model_config": "application/vnd.supernova.model.config.v1+json",
    "model_weights": "application/vnd.supernova.model.weights.v1+tar+gzip",
    "dataset_config": "application/vnd.supernova.dataset.config.v1+json",
    "dataset_content": "application/vnd.supernova.dataset.content.v1+tar+gzip",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("oras-poc")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_credentials() -> tuple[str, str]:
    if not CREDENTIALS_PATH.exists():
        sys.exit(f"Credentials file not found: {CREDENTIALS_PATH}")
    with open(CREDENTIALS_PATH) as f:
        creds = yaml.safe_load(f)
    return creds["harbor"]["username"], creds["harbor"]["password"]


def basic_auth_header(username: str, password: str) -> str:
    return b64encode(f"{username}:{password}".encode()).decode()


def sha256_file(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_dir(directory: str) -> dict[str, str]:
    checksums = {}
    for root, _, files in os.walk(directory):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, directory)
            checksums[rel] = sha256_file(fpath)
    return checksums


def create_test_model_dir(base_dir: str) -> str:
    """Create a fake HuggingFace-format model directory."""
    model_dir = os.path.join(base_dir, "test-model")
    os.makedirs(model_dir, exist_ok=True)

    with open(os.path.join(model_dir, "config.json"), "w") as f:
        json.dump(
            {
                "architectures": ["DebertaV2ForSequenceClassification"],
                "model_type": "deberta-v2",
                "hidden_size": 768,
                "num_hidden_layers": 12,
                "num_attention_heads": 12,
                "vocab_size": 32203,
                "torch_dtype": "bfloat16",
            },
            f,
            indent=2,
        )

    with open(os.path.join(model_dir, "tokenizer.json"), "w") as f:
        json.dump(
            {
                "type": "BPE",
                "vocab_size": 32203,
                "special_tokens": ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"],
            },
            f,
            indent=2,
        )

    with open(os.path.join(model_dir, "model.safetensors"), "wb") as f:
        f.write(os.urandom(1 * 1024 * 1024))  # 1MB dummy weights

    with open(os.path.join(model_dir, "training_args.json"), "w") as f:
        json.dump(
            {
                "learning_rate": 5e-5,
                "batch_size": 96,
                "max_steps": 16000,
                "optimizer": "adamw_8bit",
            },
            f,
            indent=2,
        )

    return model_dir


def create_test_large_file(base_dir: str, size_mb: int = 10) -> str:
    """Create a single large test file simulating a GGUF model."""
    fpath = os.path.join(base_dir, "test-model.gguf")
    with open(fpath, "wb") as f:
        f.writelines(os.urandom(1024 * 1024) for _ in range(size_mb))
    return fpath


def get_harbor_token(host: str, repo: str, actions: str, auth_b64: str) -> str | None:
    """Obtain a bearer token from Harbor's token service."""
    url = f"https://{host}/service/token?service=harbor-registry&scope=repository:{repo}:{actions}"
    resp = requests.get(url, headers={"Authorization": f"Basic {auth_b64}"})
    if resp.status_code == 200:
        return resp.json().get("token")
    log.warning(f"Token request failed ({resp.status_code}): {resp.text[:200]}")
    return None


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


class OrasPOC:
    def __init__(self):
        self.username, self.password = load_credentials()
        self.auth_b64 = basic_auth_header(self.username, self.password)
        self.client: oras.client.OrasClient | None = None
        self.tmpdir = tempfile.mkdtemp(prefix="oras-poc-")
        self._results: list[tuple[str, str, str]] = []
        log.info(f"Working directory: {self.tmpdir}")

    def cleanup(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)
            log.info("Cleaned up temp directory")

    def _record(self, name: str, status: str, detail: str = ""):
        self._results.append((name, status, detail))
        icon = "PASS" if status == "PASS" else "FAIL"
        msg = f"  [{icon}] {name}"
        if detail:
            msg += f" -- {detail}"
        print(msg)

    # ------------------------------------------------------------------
    # Test 1: Authentication
    # ------------------------------------------------------------------

    def test_auth(self):
        log.info("=" * 60)
        log.info("TEST: Authentication")
        log.info("=" * 60)

        # Verify credentials via Harbor v2.0 API (basic auth)
        resp = requests.get(
            f"https://{HARBOR_HOST}/api/v2.0/projects/{HARBOR_PROJECT}/repositories",
            headers={"Authorization": f"Basic {self.auth_b64}"},
        )
        log.info(f"Harbor API check: {resp.status_code}")
        if resp.status_code != 200:
            self._record(
                "auth_harbor_api",
                "FAIL",
                f"status {resp.status_code}: {resp.text[:200]}",
            )
            raise RuntimeError("Harbor API auth failed")
        self._record(
            "auth_harbor_api", "PASS", f"listed {len(resp.json())} repositories"
        )

        # Initialize oras client with token-based auth
        self.client = oras.client.OrasClient(hostname=HARBOR_HOST, auth_backend="token")
        self.client.login(
            hostname=HARBOR_HOST,
            username=self.username,
            password=self.password,
        )
        log.info(f"OrasClient logged in as {self.username}")
        self._record("auth_oras_login", "PASS", f"user={self.username}")

    # ------------------------------------------------------------------
    # Test 2: Push multi-file model directory
    # ------------------------------------------------------------------

    def test_push_multifile(self):
        log.info("=" * 60)
        log.info("TEST: Push multi-file model directory")
        log.info("=" * 60)

        model_dir = create_test_model_dir(self.tmpdir)
        files = sorted(str(f) for f in Path(model_dir).rglob("*") if f.is_file())
        log.info(
            f"Test model dir: {len(files)} files — {[os.path.basename(f) for f in files]}"
        )

        checksums_before = sha256_dir(model_dir)
        cksum_path = os.path.join(self.tmpdir, "checksums_push.json")
        with open(cksum_path, "w") as f:
            json.dump(checksums_before, f, indent=2)
        log.info(f"Pre-push checksums saved ({len(checksums_before)} files)")

        total_kb = sum(os.path.getsize(f) for f in files) / 1024

        # oras-py requires files to be relative to the working directory
        t0 = time.time()
        with oras.utils.workdir(model_dir):
            rel_files = [os.path.basename(f) for f in files]
            response = self.client.push(files=rel_files, target=TEST_MULTIFILE_REF)
        elapsed = time.time() - t0
        log.info(f"Push response: {response.status_code}")
        log.info(f"Digest: {response.headers.get('Docker-Content-Digest', 'N/A')}")
        self._record(
            "push_multifile",
            "PASS",
            f"{len(files)} files, {total_kb:.0f} KB, {elapsed:.2f}s",
        )

    # ------------------------------------------------------------------
    # Test 3: Pull and verify checksums
    # ------------------------------------------------------------------

    def test_pull_verify(self):
        log.info("=" * 60)
        log.info("TEST: Pull and verify integrity")
        log.info("=" * 60)

        pull_dir = os.path.join(self.tmpdir, "pulled-model")
        os.makedirs(pull_dir, exist_ok=True)

        t0 = time.time()
        pulled_files = self.client.pull(target=TEST_MULTIFILE_REF, outdir=pull_dir)
        elapsed = time.time() - t0
        log.info(f"Pulled {len(pulled_files)} files in {elapsed:.2f}s")
        log.info(f"Files: {pulled_files}")

        with open(os.path.join(self.tmpdir, "checksums_push.json")) as f:
            checksums_before = json.load(f)

        mismatches = []
        matched = 0
        for pulled_path in pulled_files:
            fname = os.path.basename(pulled_path)
            actual = sha256_file(pulled_path)
            expected = checksums_before.get(fname)
            if expected is None:
                log.warning(f"  EXTRA  {fname} (not in original)")
            elif actual != expected:
                mismatches.append(fname)
                log.error(f"  MISMATCH {fname}")
            else:
                matched += 1
                log.info(f"  OK     {fname}  sha256:{actual[:16]}...")

        if mismatches:
            self._record("pull_verify", "FAIL", f"mismatched: {mismatches}")
        else:
            self._record(
                "pull_verify",
                "PASS",
                f"{matched}/{len(checksums_before)} files verified",
            )

    # ------------------------------------------------------------------
    # Test 4: Push single large file
    # ------------------------------------------------------------------

    def test_push_single(self):
        log.info("=" * 60)
        log.info("TEST: Push single large file (10 MB)")
        log.info("=" * 60)

        size_mb = 10
        large_file = create_test_large_file(self.tmpdir, size_mb=size_mb)
        original_checksum = sha256_file(large_file)
        log.info(f"Created {size_mb} MB test file, sha256:{original_checksum[:16]}...")

        t0 = time.time()
        with oras.utils.workdir(self.tmpdir):
            response = self.client.push(
                files=[os.path.basename(large_file)], target=TEST_SINGLE_REF
            )
        push_elapsed = time.time() - t0
        log.info(
            f"Push: {response.status_code}, {push_elapsed:.2f}s ({size_mb / push_elapsed:.1f} MB/s)"
        )

        pull_dir = os.path.join(self.tmpdir, "pulled-single")
        os.makedirs(pull_dir, exist_ok=True)

        t0 = time.time()
        pulled = self.client.pull(target=TEST_SINGLE_REF, outdir=pull_dir)
        pull_elapsed = time.time() - t0

        if not pulled:
            self._record("push_single", "FAIL", "no files returned from pull")
            return

        pulled_checksum = sha256_file(pulled[0])
        log.info(f"Pull: {pull_elapsed:.2f}s ({size_mb / pull_elapsed:.1f} MB/s)")

        if pulled_checksum == original_checksum:
            log.info(f"Checksum verified: sha256:{pulled_checksum[:16]}...")
            self._record(
                "push_single",
                "PASS",
                f"{size_mb} MB, push={push_elapsed:.2f}s, pull={pull_elapsed:.2f}s, checksum OK",
            )
        else:
            self._record("push_single", "FAIL", "checksum mismatch")

    # ------------------------------------------------------------------
    # Test 5: HEAD requests for artifact existence
    # ------------------------------------------------------------------

    def test_head_request(self):
        log.info("=" * 60)
        log.info("TEST: HEAD request — artifact existence check")
        log.info("=" * 60)

        results = {}

        # --- OCI Distribution: HEAD /v2/<repo>/manifests/<tag> ---
        test_cases = [
            ("test-model:v1 (exists)", f"{HARBOR_PROJECT}/test-model", "v1"),
            ("nonexistent:v999 (missing)", f"{HARBOR_PROJECT}/nonexistent", "v999"),
        ]

        for label, repo, tag in test_cases:
            token = get_harbor_token(HARBOR_HOST, repo, "pull", self.auth_b64)
            if not token:
                results[label] = "token_error"
                continue

            url = f"https://{HARBOR_HOST}/v2/{repo}/manifests/{tag}"
            resp = requests.head(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.oci.image.manifest.v1+json",
                },
            )
            results[label] = resp.status_code
            digest = resp.headers.get("Docker-Content-Digest", "N/A")
            length = resp.headers.get("Content-Length", "N/A")
            log.info(
                f"  HEAD {label}: {resp.status_code}  digest={digest}  length={length}"
            )

        # --- Harbor v2.0 API (basic auth, supports GET) ---
        log.info("")
        for artifact, label in [
            ("test-model", "harbor_api_exists"),
            ("nonexistent", "harbor_api_missing"),
        ]:
            url = f"https://{HARBOR_HOST}/api/v2.0/projects/{HARBOR_PROJECT}/repositories/{artifact}/artifacts"
            resp = requests.get(
                url, headers={"Authorization": f"Basic {self.auth_b64}"}
            )
            results[label] = resp.status_code
            count = len(resp.json()) if resp.status_code == 200 else 0
            log.info(f"  GET  {label}: {resp.status_code}  artifacts={count}")

        existing_ok = results.get("test-model:v1 (exists)") == 200
        missing_ok = results.get("nonexistent:v999 (missing)") in (
            404,
            401,
            "token_error",
        )
        if existing_ok and missing_ok:
            self._record(
                "head_request",
                "PASS",
                f"existing=200, missing={results.get('nonexistent:v999 (missing)')}",
            )
        else:
            self._record("head_request", "FAIL", f"results={results}")

    # ------------------------------------------------------------------
    # Test 6: Custom SuperNova media types
    # ------------------------------------------------------------------

    def test_custom_media(self):
        log.info("=" * 60)
        log.info("TEST: Custom media types (SuperNova pattern)")
        log.info("=" * 60)

        model_dir = create_test_model_dir(os.path.join(self.tmpdir, "custom-media"))

        config_data = {
            "artifact_type": "model",
            "name": "test-custom-media",
            "version": "v1",
            "model_type": "deberta-v2",
            "format": "safetensors",
        }

        # Write config to a temp file
        config_path = os.path.join(self.tmpdir, "supernova-config.json")
        with open(config_path, "w") as f:
            json.dump(config_data, f)

        # Build OCI manifest with custom media types
        manifest = oras.oci.NewManifest()

        # Config blob with SuperNova media type
        conf, _ = oras.oci.ManifestConfig(config_path)
        conf["mediaType"] = MEDIA_TYPES["model_config"]

        # Tar the model directory into a single weights layer
        blob = oras.utils.make_targz(model_dir)
        layer = oras.oci.NewLayer(blob, MEDIA_TYPES["model_weights"], is_dir=True)
        layer["annotations"] = {
            oras.defaults.annotation_title: "model.tar.gz",
            "org.opencontainers.image.created": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
        }
        manifest["layers"].append(layer)
        manifest["config"] = conf
        manifest["annotations"] = {
            "org.opencontainers.image.created": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
        }

        # OrasClient IS a Registry — use its methods directly
        container = self.client.get_container(TEST_CUSTOM_MT_REF)

        t0 = time.time()
        resp = self.client.upload_blob(config_path, container, conf)
        self.client._check_200_response(resp)
        log.info(f"Uploaded config blob ({conf['size']} bytes)")

        resp = self.client.upload_blob(blob, container, layer)
        self.client._check_200_response(resp)
        log.info(f"Uploaded weights layer ({layer['size']} bytes)")

        resp = self.client.upload_manifest(manifest, container)
        self.client._check_200_response(resp)
        elapsed = time.time() - t0
        log.info(f"Manifest uploaded in {elapsed:.2f}s")

        if os.path.exists(blob):
            os.remove(blob)

        # Verify: fetch manifest and check media types are preserved
        fetched = self.client.get_manifest(container)
        config_mt = fetched.get("config", {}).get("mediaType", "")
        layer_mts = [layer.get("mediaType", "") for layer in fetched.get("layers", [])]
        log.info(f"Fetched manifest — config mediaType: {config_mt}")
        log.info(f"Fetched manifest — layer mediaTypes:  {layer_mts}")

        config_ok = config_mt == MEDIA_TYPES["model_config"]
        weights_ok = MEDIA_TYPES["model_weights"] in layer_mts
        if config_ok and weights_ok:
            self._record(
                "custom_media", "PASS", f"config={config_mt}, layers={layer_mts}"
            )
        else:
            self._record(
                "custom_media", "FAIL", f"config={config_mt}, layers={layer_mts}"
            )

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------

    def print_summary(self) -> bool:
        print("\n" + "=" * 60)
        print("RESULTS SUMMARY")
        print("=" * 60)
        all_pass = True
        for name, status, detail in self._results:
            icon = "PASS" if status == "PASS" else "FAIL"
            line = f"  [{icon}] {name}"
            if detail:
                line += f" -- {detail}"
            print(line)
            if status != "PASS":
                all_pass = False
        print("=" * 60)
        print(f"  {'ALL TESTS PASSED' if all_pass else 'SOME TESTS FAILED'}")
        print("=" * 60)
        return all_pass

    def run_all(self) -> bool:
        tests = [
            self.test_auth,
            self.test_push_multifile,
            self.test_pull_verify,
            self.test_push_single,
            self.test_head_request,
            self.test_custom_media,
        ]
        for fn in tests:
            try:
                fn()
            except Exception:
                log.exception(f"Test {fn.__name__} raised an exception")
        return self.print_summary()

    def run_test(self, name: str):
        test_map = {
            "auth": self.test_auth,
            "push_multifile": self.test_push_multifile,
            "pull_verify": self.test_pull_verify,
            "push_single": self.test_push_single,
            "head_request": self.test_head_request,
            "custom_media": self.test_custom_media,
        }
        if name not in test_map:
            sys.exit(f"Unknown test: {name}. Available: {list(test_map.keys())}")
        if name != "auth":
            self.test_auth()
        test_map[name]()
        return self.print_summary()


def main():
    parser = argparse.ArgumentParser(description="D-003: ORAS Python Library POC")
    parser.add_argument("--test", default="all", help="Test to run (default: all)")
    parser.add_argument(
        "--keep-temp", action="store_true", help="Keep temp directory after run"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        from oras.logger import setup_logger

        setup_logger(quiet=False, debug=True)
        logging.getLogger().setLevel(logging.DEBUG)

    poc = OrasPOC()
    try:
        if args.test == "all":
            success = poc.run_all()
        else:
            success = poc.run_test(args.test)
    finally:
        if not args.keep_temp:
            poc.cleanup()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
