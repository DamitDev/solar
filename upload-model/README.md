# supernova-step-upload-model

Step image for SuperNova training pipelines. Pushes a trained model artifact
from the ephemeral job workspace to Harbor via ORAS and registers the resulting
version with the Data Repository.

Harbor stores the blobs; Data Repository stores metadata and verifies the
pushed artifact. The step pushes directly to Harbor using `harbor-oci-client`
/ ORAS and does not rely on Solar Control for blob transfer.

## Workspace Contract

This step conforms to the [Job Step Workspace
specification](https://github.com/DamitDev/training-platform-project/blob/master/docs/specs/job-step-workspace.md)
(S-021).

- **Consumes:** `MODEL_SOURCE_PATH`, `HARBOR_TARGET_REF`, `ARTIFACT_NAME`,
  `VERSION`, `ARTIFACT_CATEGORY`, `METADATA_PATH`, plus workspace paths and
  infrastructure credentials.
- **Reads previous step results:** `job.json` → `steps.train.best_checkpoint_path`
  (or `steps.convert_model.*`) to determine what to upload.
- **Produces:** A pushed OCI artifact in Harbor and a registered version in the
  Data Repository.
- **Side effect:** Atomically updates `/workspace/config/job.json` →
  `steps.upload_model`.

## Required Environment Variables

| Variable               | Example                                           | Description                                     |
| ---------------------- | ------------------------------------------------- | ----------------------------------------------- |
| `MODEL_SOURCE_PATH`    | `/workspace/output/base_osl.gguf`                 | Path to the model artifact to upload (file or directory) |
| `HARBOR_TARGET_REF`    | `imgrepo.damit.hu/supernova/iris-osl:v4`          | Target Harbor OCI reference                     |
| `ARTIFACT_NAME`        | `iris-osl`                                        | Data Repository artifact name                   |
| `VERSION`              | `v4`                                              | Version string for registration (optional; auto-incremented if omitted) |
| `ARTIFACT_CATEGORY`    | `model`                                           | `model` or `dataset`                            |
| `METADATA_PATH`        | `/workspace/config/upload-metadata.json`          | Path to metadata JSON for registration (optional; an absent file is skipped with a warning) |
| `DATA_REPOSITORY_URL`  | `http://data-repository:8000`                     | Data Repository API base URL                    |
| `HARBOR_URL`           | `https://imgrepo.damit.hu`                        | Harbor registry URL                             |
| `HARBOR_USERNAME`      | `robot_supernova+aiops`                           | Harbor robot account                            |
| `HARBOR_PASSWORD`      | `...`                                             | Harbor robot password                           |
| `HARBOR_OPERATION_TIMEOUT_SECONDS` | `300`                                  | Optional maximum duration for Harbor login and push |

## Building

```bash
docker build -f upload-model/Dockerfile -t supernova-step-upload-model .
```

The image installs `certs/damit-cloud-root-ca.crt` into its system trust store,
so Data Repository and Harbor endpoints under `*.damit.cloud` are verified
without disabling TLS validation.

## Local Smoke Test

This test pushes a real model artifact to Harbor and registers it (requires
Harbor access and a running Data Repository).

```bash
# 1. Build the image
docker build -f upload-model/Dockerfile -t supernova-step-upload-model .

# 2. Create a temporary workspace for testing
TEST_WS=$(mktemp -d)
mkdir -p "$TEST_WS"/{output,config}
echo '{"job_id":"smoke-test","name":"smoke","steps":{}}' > "$TEST_WS"/config/job.json
echo '{"training_config":{"epochs":3}}' > "$TEST_WS"/config/upload-metadata.json
echo "fake-model-bytes" > "$TEST_WS"/output/base_osl.gguf

# 3. Run the step
docker run --rm \
  -v "$TEST_WS/output:/workspace/output" \
  -v "$TEST_WS/config:/workspace/config" \
  -e MODEL_SOURCE_PATH="/workspace/output/base_osl.gguf" \
  -e HARBOR_TARGET_REF="imgrepo.damit.hu/supernova/iris-osl:smoke-test" \
  -e ARTIFACT_NAME="iris-osl" \
  -e VERSION="smoke-test" \
  -e ARTIFACT_CATEGORY="model" \
  -e METADATA_PATH="/workspace/config/upload-metadata.json" \
  -e DATA_REPOSITORY_URL \
  -e HARBOR_URL \
  -e HARBOR_USERNAME \
  -e HARBOR_PASSWORD \
  supernova-step-upload-model

# 4. Verify
echo "Exit code: $?"
cat "$TEST_WS"/config/job.json | python3 -m json.tool

# 5. Cleanup
rm -rf "$TEST_WS"
```

## Error Handling

| Failure                            | Exit Code | Message                                          |
| ---------------------------------- | --------- | ------------------------------------------------ |
| Missing `MODEL_SOURCE_PATH`        | 1         | `MODEL_SOURCE_PATH is required`                  |
| Missing `HARBOR_TARGET_REF`        | 1         | `HARBOR_TARGET_REF is required`                  |
| Missing `ARTIFACT_NAME`            | 1         | `ARTIFACT_NAME is required`                      |
| Invalid `ARTIFACT_CATEGORY`        | 1         | `ARTIFACT_CATEGORY must be one of ...`           |
| Path outside `/workspace/output/`  | 1         | `MODEL_SOURCE_PATH (...) must be under ...`      |
| Source does not exist              | 1         | `MODEL_SOURCE_PATH does not exist: ...`          |
| Source contains no files           | 1         | `MODEL_SOURCE_PATH contains no files ...`        |
| Ref host differs from `HARBOR_URL` | 1         | `HARBOR_TARGET_REF (...) targets registry ...`   |
| Malformed `HARBOR_TARGET_REF`      | 1         | `HARBOR_TARGET_REF (...) must be a full OCI reference` |
| Malformed `METADATA_PATH` contents | 1         | `METADATA_PATH ... is not valid JSON`            |
| Harbor auth failure (401)          | 1         | `ORAS push failed for ...`                       |
| Harbor login/push timeout          | 1         | `ORAS operation timed out after ...`             |
| ORAS push failure                  | 1         | `ORAS push failed for ...`                       |
| Data Repository unreachable        | 1         | `Failed to reach Data Repository at ...`         |
| Registration conflict (409)        | 1         | `version already exists or category conflict`    |
| Harbor artifact not found (404)    | 1         | `Data Repository could not verify ... in Harbor` |
| `job.json` not found               | 1         | `job.json not found at ...`                      |
| Success                            | 0         | `job.json updated successfully`                  |

The source artifact is pushed to Harbor via `OrasHelper.push_custom`, which
tars the content into a single layer using the SuperNova media types for the
artifact category. The resulting digest is passed to the Data Repository
registration call along with the aggregated metadata.

`size_bytes` is deliberately left out of the registration payload. The pushed
artifact is a gzipped tar, so the uncompressed source size would not describe
what Harbor actually stores; the Data Repository resolves the authoritative
size from Harbor when the field is absent. The uncompressed source size is
still recorded in `job.json` → `steps.upload_model.size_bytes`.

If registration succeeds but the `job.json` update fails, the step exits 1 and
logs the registered version and Harbor reference. The Data Repository version
is immutable and cannot be rolled back, so re-running the step as-is will fail
with a version conflict — resume the pipeline manually from that point.

## Metadata Aggregation

The step combines `upload-metadata.json` from `/workspace/config/` (produced by
the train step) with information from `job.json` when constructing the Data
Repository registration payload:

- **Lineage:** `source_trainer` defaults to the job ID from `job.json` unless
  the metadata file already provides it.
- **Eval metrics:** falls back to `steps.train.eval_metrics` from `job.json`
  when the metadata file does not define `eval_metrics`.

The metadata follows the conventions in the Data Repository
[`schema.md`](https://github.com/DamitDev/data-repository/blob/master/docs/schema.md)
(`training_config`, `model_config`, `eval_metrics`, `lineage`).
