"""intent_path: deployment strategies + delete semantics (marker: intent_path).

Spec changes (via PUT /api/intents/{id}, spec §12.5) trigger REPLACE under
the intent's strategy — a new model_source or an edited backend config
alone; delete stops managed instances; delete with ?orphan=true keeps them
running with markers cleared.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fixtures.constants import MODEL_NAME
from fixtures.helpers import wait_for
from fixtures.intents import (
    classify_until_ok,
    create_intent,
    get_intent,
    replica_states,
    update_intent,
    wait_intent_ready,
)
from fixtures.seed import read_test_model_files

pytestmark = pytest.mark.intent_path


def _alias(prefix: str = "strat") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _register_v2(stack, http_data_repo) -> str:
    """Register test-model:v2 (different artifact) in stub Harbor + data-repo.

    Idempotent: v2 is shared across tests in the module. The v2 artifact
    is a *valid* safetensors file (tensors bit-identical to v1, only the
    header metadata differs -> different sha256 -> different artifact
    identity), so v2 replicas actually serve inference instead of
    crashing at startup.
    """
    from fixtures.constants import FIXTURE_MODEL_DIR, harbor_port
    from fixtures.helpers import rewrite_safetensors_with_metadata

    v2_ref = f"127.0.0.1:{harbor_port(stack.harbor_ref)}/supernova/{MODEL_NAME}:v2"
    resp = await http_data_repo.get(f"/api/models/{MODEL_NAME}/versions")
    if resp.status_code == 200 and any(
        v["version"] == "v2" for v in resp.json().get("versions", [])
    ):
        return f"repo://{MODEL_NAME}:v2"
    files = read_test_model_files(FIXTURE_MODEL_DIR)
    # Different but VALID content: re-save the same tensors with a
    # "version: v2" header entry. Appending bytes to a safetensors file
    # (the old construction) makes it longer than the header's
    # data_offsets coverage -> every v2 instance crashed on start with
    # SafetensorError ("incomplete metadata, file not fully covered").
    files = dict(files)
    files["model.safetensors"] = rewrite_safetensors_with_metadata(
        files["model.safetensors"], {"version": "v2"}
    )
    stack.stub_harbor.register_model(v2_ref, files)
    resp = await http_data_repo.post(
        f"/api/models/{MODEL_NAME}/versions",
        json={"harbor_ref": v2_ref, "version": "v2"},
    )
    assert resp.status_code == 201, resp.text
    return f"repo://{MODEL_NAME}:v2"


async def _wait_model_source(http_control, intent_id: str, source: str) -> dict:
    """Poll until every running replica carries the new model_source."""

    async def migrated() -> bool:
        intent = await get_intent(http_control, intent_id)
        if intent is None or intent["status"]["phase"] != "ready":
            return False
        replicas = intent["status"].get("replica_set", [])
        if not replicas:
            return False
        return all(r.get("model_source") == source for r in replicas)

    await wait_for(
        migrated, timeout=180.0, interval=0.5, description=f"intent on {source}"
    )
    intent = await get_intent(http_control, intent_id)
    assert intent is not None
    return intent


async def test_rolling_version_change(http_control, http_data_repo, stack, clean_state):
    """PUT model_source -> old replica replaced by new, intent ready again."""
    v2_source = await _register_v2(stack, http_data_repo)
    intent = await create_intent(http_control, alias=_alias())
    await wait_intent_ready(http_control, intent["id"])
    ready = await get_intent(http_control, intent["id"])
    assert ready is not None
    old_instance_id = next(iter(replica_states(ready)))

    updated = await update_intent(http_control, ready, model_source=v2_source)
    assert updated["model_source"] == v2_source
    assert updated["status"]["strategy_progress"] is None

    final = await _wait_model_source(http_control, intent["id"], v2_source)
    status = final["status"]
    assert status["phase"] == "ready"
    assert status["ready_replicas"] == 1
    assert status["updated_replicas"] == 1
    # The old instance is gone; the new one runs the v2 source.
    new_instance_ids = set(replica_states(final))
    assert old_instance_id not in new_instance_ids
    assert len(new_instance_ids) == 1
    assert final["status"]["replica_set"][0]["model_source"] == v2_source

    # The v2 replica must actually serve inference (the old fixture's
    # corrupt safetensors made every v2 replica a dead server).
    body = await classify_until_ok(
        http_control, final["alias"], stack=stack, timeout=30.0
    )
    assert body["model"] == final["alias"]
    assert len(body["choices"]) == 1
    assert body["choices"][0]["score"] > 0.0


async def test_immediate_version_change(
    http_control, http_data_repo, stack, clean_state
):
    """Same via strategy=immediate: converges to the new version, ready."""
    v2_source = await _register_v2(stack, http_data_repo)
    intent = await create_intent(http_control, alias=_alias(), strategy="immediate")
    ready = await wait_intent_ready(http_control, intent["id"])

    await update_intent(http_control, ready, model_source=v2_source)

    final = await _wait_model_source(http_control, intent["id"], v2_source)
    assert final["status"]["phase"] == "ready"
    assert final["status"]["ready_replicas"] == 1
    assert final["status"]["updated_replicas"] == 1

    # Liveness of the v2 replica (positive assertion per house rules).
    body = await classify_until_ok(
        http_control, final["alias"], stack=stack, timeout=30.0
    )
    assert body["model"] == final["alias"]
    assert body["choices"][0]["score"] > 0.0


async def test_rolling_backend_config_change(http_control, stack, clean_state):
    """PUT backend config only -> replica replaced under the strategy.

    The spec keeps its model_source, so nothing about the version changed.
    A rollout that recognised drift by comparing model_source found nothing
    to replace here, and the replica was left stopped with the alias down.
    """
    intent = await create_intent(http_control, alias=_alias("cfg"))
    ready = await wait_intent_ready(http_control, intent["id"])
    old_instance_id = next(iter(replica_states(ready)))

    backend = dict(ready["backend"])
    backend["max_length"] = 256
    updated = await update_intent(http_control, ready, backend=backend)
    assert updated["backend"]["max_length"] == 256
    assert updated["model_source"] == ready["model_source"]

    async def replaced() -> bool:
        state = await get_intent(http_control, intent["id"])
        if state is None or state["status"]["phase"] != "ready":
            return False
        replicas = replica_states(state)
        return bool(replicas) and old_instance_id not in replicas

    try:
        await wait_for(
            replaced,
            timeout=180.0,
            interval=0.5,
            description="replica replaced for the edited backend config",
        )
    except AssertionError as exc:
        state = await get_intent(http_control, intent["id"])
        raise AssertionError(
            f"config change never rolled out; intent={state}\n{stack.tail()}"
        ) from exc

    # spec_changed_at clears only when the reconciler compares the live
    # instance config against the new spec and finds no drift left — that is
    # the proof the replacement actually carries the edited config.
    await wait_for(
        lambda: _spec_settled(http_control, intent["id"]),
        timeout=60.0,
        interval=0.5,
        description="edited spec settled on the replicas",
    )

    final = await wait_intent_ready(http_control, intent["id"], timeout=180.0)
    assert final["status"]["updated_replicas"] == 1

    body = await classify_until_ok(
        http_control, final["alias"], stack=stack, timeout=30.0
    )
    assert body["choices"][0]["score"] > 0.0


async def test_edit_is_not_lost_while_the_host_is_unreachable(
    http_control, stack, clean_state
):
    """An edit the reconciler cannot verify stays pending until it can.

    Drift in backend config is only visible in the replica's real
    configuration, which lives on the host — the cached instance view carries
    almost none of those fields. When that read fails, "no drift" is not a
    finding: concluding it would declare the edit rolled out and drop it, and
    the replica would serve the old config while the intent reported itself up
    to date.

    Failure injection: the host's API key is rotated in the DB, so control's
    HTTP calls fail while its WS channel (the reconciler's view of instances)
    stays intact — the lever the RECREATE failure test uses.
    """
    from fixtures.faults import broken_host_api_key

    intent = await create_intent(http_control, alias=_alias("unreachable"))
    ready = await wait_intent_ready(http_control, intent["id"])
    old_instance_id = next(iter(replica_states(ready)))
    host_id = ready["status"]["replica_set"][0]["host_id"]

    hosts = (await http_control.get("/api/hosts")).json()
    host_name = next(h["name"] for h in hosts if h["id"] == host_id)

    with broken_host_api_key(stack, host_id, host_name):
        backend = dict(ready["backend"])
        backend["max_length"] = 256
        updated = await update_intent(http_control, ready, backend=backend)
        # PUT stamps the marker only when the submitted spec differs from the
        # stored one, so a bare "is not None" tells you nothing when it fires
        # (2026-08-12: it fired once in five runs and cost a forensic dig).
        # Print both sides — the comparison is the thing under suspicion.
        assert updated["status"]["spec_changed_at"] is not None, (
            "PUT did not stamp spec_changed_at, so the submitted spec "
            "compared equal to the stored one\n"
            f"submitted backend: {backend}\n"
            f"returned backend:  {updated['backend']}\n"
            f"returned status:   {updated['status']}"
        )

        # Several ticks pass with the host unreachable: the edit must still be
        # pending, and the replica it applies to must still be the old one.
        await asyncio.sleep(8)
        stalled = await get_intent(http_control, intent["id"])
        assert stalled is not None
        assert stalled["status"]["spec_changed_at"] is not None, (
            "the edit was declared rolled out while the host could not be read: "
            f"{stalled['status']}\n{stack.tail()}"
        )
        assert old_instance_id in replica_states(stalled)

    # The key is restored on block exit: the rollout runs and the edit applies.

    async def replaced() -> bool:
        state = await get_intent(http_control, intent["id"])
        if state is None or state["status"]["phase"] != "ready":
            return False
        replicas = replica_states(state)
        return bool(replicas) and old_instance_id not in replicas

    try:
        await wait_for(
            replaced,
            timeout=180.0,
            interval=0.5,
            description="edit rolled out once the host was reachable",
        )
    except AssertionError as exc:
        state = await get_intent(http_control, intent["id"])
        raise AssertionError(
            f"edit never rolled out after recovery; intent={state}\n{stack.tail()}"
        ) from exc

    await wait_for(
        lambda: _spec_settled(http_control, intent["id"]),
        timeout=60.0,
        interval=0.5,
        description="edited spec settled after recovery",
    )


async def _spec_settled(http_control, intent_id: str) -> bool:
    state = await get_intent(http_control, intent_id)
    return state is not None and state["status"]["spec_changed_at"] is None


async def test_delete_intent_cleans_up(http_control, clean_state):
    """Delete -> managed instances stopped+deleted, phase deleted, alias gone."""
    intent = await create_intent(http_control, alias=_alias())
    ready = await wait_intent_ready(http_control, intent["id"])
    instance_id = next(iter(replica_states(ready)))
    host_id = ready["status"]["replica_set"][0]["host_id"]

    resp = await http_control.delete(f"/api/intents/{intent['id']}")
    assert resp.status_code == 202, resp.text

    # Soft-deleted intents are hidden by the API (404) once the reconciler
    # finishes cleanup — poll for that, then assert the host-side cleanup.
    await wait_for(
        lambda: _soft_deleted(http_control, intent["id"]),
        timeout=30.0,
        interval=0.5,
        description="intent soft-deleted (API 404)",
    )

    # Instance removed from the host.
    hosts_resp = await http_control.get(f"/api/hosts/{host_id}/instances")
    assert hosts_resp.status_code == 200
    assert instance_id not in [i["id"] for i in hosts_resp.json()]

    # Alias gone from the gateway registry.
    await wait_for(
        lambda: _alias_gone(http_control, ready["alias"]),
        timeout=60.0,
        interval=0.5,
        description="alias removed from registry",
    )


async def _soft_deleted(http_control, intent_id: str) -> bool:
    return (await get_intent(http_control, intent_id)) is None


async def _alias_gone(http_control, alias: str) -> bool:
    return not await _alias_visible(http_control, alias)


async def _alias_visible(http_control, alias: str) -> bool:
    resp = await http_control.get("/v1/models")
    if resp.status_code != 200:
        return False
    body = resp.json()
    names = {m.get("name") for m in body.get("models", [])} | {
        m.get("id") for m in body.get("data", [])
    }
    return alias in names


async def test_delete_orphan_keeps_instances(http_control, stack, clean_state):
    """DELETE ?orphan=true -> instances keep running, markers cleared in cache."""
    from fixtures.seed import redis_cache_instances

    intent = await create_intent(http_control, alias=_alias())
    ready = await wait_intent_ready(http_control, intent["id"])
    instance_id = next(iter(replica_states(ready)))
    host_id = ready["status"]["replica_set"][0]["host_id"]

    resp = await http_control.delete(f"/api/intents/{intent['id']}?orphan=true")
    assert resp.status_code == 202, resp.text

    # The reconciler disowns the managed instance (markers cleared in the
    # Redis cache — the reconciler's view) and then transitions the intent
    # to 'deleted' (deleted_at set) — the API 404s. The disown chain spans
    # several passes, and the sequential loop may be held by a strategy
    # health gate, so this wait gets headroom beyond the fast-path 15s.
    await wait_for(
        lambda: _soft_deleted(http_control, intent["id"]),
        timeout=45.0,
        interval=0.5,
        description="intent soft-deleted (API 404)",
    )
    cached = redis_cache_instances(stack.db_env["redis"], host_id)
    assert cached, "instance still present in the cache"
    inst = next(
        i for i in cached if (i.get("id") or i.get("instance_id")) == instance_id
    )
    assert inst.get("managed_by") not in ("intent",)
    assert inst.get("intent_id") not in (intent["id"],)

    # The instance is still there and still running (orphaned). The host's
    # own config retains the markers (no host-side PATCH for running
    # instances) — only the control-side view is cleared.
    hosts_resp = await http_control.get(f"/api/hosts/{host_id}/instances")
    assert hosts_resp.status_code == 200
    instances = hosts_resp.json()
    inst = next(i for i in instances if i["id"] == instance_id)
    assert inst["status"] == "running"
