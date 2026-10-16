"""BOD80 lane C: Hermes v0.21.3 native plugin memory adapter contract.

Hook surfaces are SOURCE-EVIDENCED (read from installed Hermes source), not yet
observed live. All backends here are fakes; the P client is under security hold.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from verdict import harness_hermes_memory as hm

FIXTURES = Path(__file__).parent / "fixtures" / "hermes" / "v0_21_3"
OPERATOR_LIKE_CONFIG = {
    "model": {"provider": "custom", "default": "grok-4.7"},
    "memory": {"memory_enabled": True, "user_profile_enabled": True},
    "plugins": {"enabled": ["orca-status"]},
}


def _fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    data.pop("_source", None)
    return data


def _home(tmp_path: Path, config: dict[str, Any] | None = None) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(config if config is not None else OPERATOR_LIKE_CONFIG), encoding="utf-8"
    )
    return home


class FakeProvider:
    """Project-scoped fake store. Authority = configured project, never hook payload."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.queries: list[tuple[str, str, str]] = []

    def recall(self, *, project: str, workspace: str, query: str, limit: int) -> hm.RecallOutcome:
        self.queries.append((project, workspace, query))
        if self.down:
            return hm.RecallOutcome(status="error", hits=(), error="backend_unreachable")
        hits = tuple(self.records.get((project, workspace), ())[:limit])
        return hm.RecallOutcome(status="ok" if hits else "empty", hits=hits)

    def capture(self, event: dict[str, Any]) -> bool:
        if self.down:
            raise ConnectionError("backend down")
        key = (event["identity"]["project"], event["identity"]["workspace"])
        bucket = self.records.setdefault(key, [])
        sid = event["identity"]["source_event_id"]
        if all(r["identity"]["source_event_id"] != sid for r in bucket):
            bucket.append(event)
        return True


class FakeAdmission:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def record_backend_failure(self, reason: str) -> None:
        self.failures.append(reason)

    def admit_new_work(self) -> bool:
        return not self.failures


# 1. certify correction lives in tests/test_harness_hermes.py (owned edit).


# 2. install: idempotent, preserves orca-status / memory / model, temp HERMES_HOME only
def test_install_is_idempotent_and_preserves_unrelated_config(tmp_path: Path) -> None:
    home = _home(tmp_path)
    first = hm.install(hermes_home=home, command=[sys.executable, "-m", "verdict.harness_hermes_memory"])
    assert first.changed is True
    cfg_after_first = (home / "config.yaml").read_bytes()
    plugin_dir = home / "plugins" / hm.PLUGIN_NAME
    assert (plugin_dir / "plugin.yaml").is_file()
    assert (plugin_dir / "__init__.py").is_file()
    assert (plugin_dir / hm.OWNERSHIP_MARKER).is_file()
    manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == hm.PLUGIN_NAME
    assert set(manifest["provides_hooks"]) == set(hm.HOOKS_USED)

    data = yaml.safe_load(cfg_after_first)
    assert data["plugins"]["enabled"] == ["orca-status", hm.PLUGIN_NAME]
    assert data["memory"] == OPERATOR_LIKE_CONFIG["memory"]
    assert "provider" not in data["memory"]  # never takes the single memory.provider slot
    assert data["model"] == OPERATOR_LIKE_CONFIG["model"]

    second = hm.install(hermes_home=home, command=[sys.executable, "-m", "verdict.harness_hermes_memory"])
    assert second.changed is False
    assert (home / "config.yaml").read_bytes() == cfg_after_first
    assert sorted(p.name for p in home.iterdir() if p.name.endswith(".bak")) == [hm.BACKUP_NAME]


# 3. uninstall removes only owned entries; user collision refused
def test_uninstall_restores_only_owned_entries(tmp_path: Path) -> None:
    home = _home(tmp_path)
    before = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    hm.install(hermes_home=home)
    result = hm.uninstall(hermes_home=home)
    assert result.removed is True
    assert not (home / "plugins" / hm.PLUGIN_NAME).exists()
    assert yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) == before
    assert hm.uninstall(hermes_home=home).removed is False


def test_uninstall_drops_plugins_key_it_created(tmp_path: Path) -> None:
    home = _home(tmp_path, {"model": {"default": "x"}})
    hm.install(hermes_home=home)
    hm.uninstall(hermes_home=home)
    assert yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) == {"model": {"default": "x"}}


def test_install_refuses_user_owned_plugin_collision(tmp_path: Path) -> None:
    home = _home(tmp_path)
    user_dir = home / "plugins" / hm.PLUGIN_NAME
    user_dir.mkdir(parents=True)
    (user_dir / "plugin.yaml").write_text("name: verdict-memory\n# user's own\n", encoding="utf-8")
    cfg_before = (home / "config.yaml").read_bytes()
    with pytest.raises(hm.HermesMemoryCollisionError):
        hm.install(hermes_home=home)
    with pytest.raises(hm.HermesMemoryCollisionError):
        hm.uninstall(hermes_home=home)
    assert (user_dir / "plugin.yaml").read_text(encoding="utf-8").endswith("# user's own\n")
    assert (home / "config.yaml").read_bytes() == cfg_before


def test_installed_plugin_registers_only_valid_source_hooks(tmp_path: Path) -> None:
    home = _home(tmp_path)
    hm.install(hermes_home=home)
    init = home / "plugins" / hm.PLUGIN_NAME / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_verdict_memory_plugin_test", init)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    registered: dict[str, Any] = {}

    class Ctx:
        def register_hook(self, name: str, cb: Any) -> None:
            registered[name] = cb

    module.register(Ctx())
    surface = json.loads((FIXTURES / "source_surface.json").read_text(encoding="utf-8"))
    assert set(registered) == set(surface["valid_hooks_used"]) == set(hm.HOOKS_USED)


# 4. pre_llm_call -> canonical before-turn event; bounded, cited, untrusted recall
def test_pre_llm_call_translates_to_canonical_event() -> None:
    event = hm.translate_hook("pre_llm_call", _fixture("pre_llm_call.json"), project="proj-a")
    assert event["schema_version"] == "lifecycle-event/v1"
    assert event["kind"] == "before_first_model_turn"
    ident = event["identity"]
    assert ident["source_harness"] == "hermes"
    assert ident["project"] == "proj-a"
    assert ident["workspace"] == "default"
    assert ident["session_id"] == "20261016_190000_abc123"
    assert ident["source_event_id"] == "hermes:20261016_190000_abc123:t-1:pre_llm_call"
    assert len(event["content_hash"]) == 64
    again = hm.translate_hook("pre_llm_call", _fixture("pre_llm_call.json"), project="proj-a")
    assert hm.logical_digest(event) == hm.logical_digest(again)


def test_unsupported_hook_is_explicit() -> None:
    with pytest.raises(ValueError, match="unsupported hermes hook"):
        hm.translate_hook("pre_tool_call", {"session_id": "s"}, project="p")


def test_recall_is_bounded_cited_untrusted_and_empty_is_not_error(tmp_path: Path) -> None:
    provider = FakeProvider()
    bridge = hm.HermesMemoryBridge(
        provider=provider, outbox=hm.BoundedJsonlOutbox(tmp_path / "o.jsonl", max_events=5),
        project="proj-a", max_context_chars=400,
    )
    empty = bridge.handle("pre_llm_call", _fixture("pre_llm_call.json"))
    assert empty.status == "empty" and empty.context is None and empty.gaps == ()

    provider.records[("proj-a", "default")] = [
        {"identity": {"source_event_id": f"e{i}"}, "content": "remember: " + "x" * 300}
        for i in range(5)
    ]
    resp = bridge.handle("pre_llm_call", _fixture("pre_llm_call.json"))
    assert resp.status == "ok"
    assert resp.context is not None and len(resp.context) <= 400
    assert "untrusted" in resp.context.lower()
    assert "[e0]" in resp.context


# 5. post_llm_call -> capture, redacted, one logical event per source_event_id
def test_post_llm_call_capture_redacts_and_dedupes(tmp_path: Path) -> None:
    provider = FakeProvider()
    bridge = hm.HermesMemoryBridge(
        provider=provider, outbox=hm.BoundedJsonlOutbox(tmp_path / "o.jsonl", max_events=5),
        project="proj-a",
    )
    payload = _fixture("post_llm_call.json")
    bridge.handle("post_llm_call", payload)
    bridge.handle("post_llm_call", payload)
    stored = provider.records[("proj-a", "default")]
    assert len(stored) == 1
    assert "SHOULDNOTLEAK" not in json.dumps(stored)
    assert stored[0]["kind"] == "before_yield"
    assert stored[0]["identity"]["source_event_id"] == "hermes:20261016_190000_abc123:t-1:post_llm_call"


# 6. two projects/sessions cannot recall each other; payload cannot claim project
def test_cross_project_isolation_and_payload_cannot_claim_project(tmp_path: Path) -> None:
    provider = FakeProvider()
    a = hm.HermesMemoryBridge(provider=provider, outbox=hm.BoundedJsonlOutbox(tmp_path / "a", max_events=5), project="proj-a")
    b = hm.HermesMemoryBridge(provider=provider, outbox=hm.BoundedJsonlOutbox(tmp_path / "b", max_events=5), project="proj-b")
    hostile = dict(_fixture("post_llm_call.json"), project="proj-b", workspace="evil")
    a.handle("post_llm_call", hostile)
    assert ("proj-b", "default") not in provider.records
    assert ("proj-b", "evil") not in provider.records
    assert b.handle("pre_llm_call", dict(_fixture("pre_llm_call.json"), project="proj-a")).status == "empty"
    assert provider.queries[-1][:2] == ("proj-b", "default")


# 7. backend failure: harness degrades with named gap, bounded outbox capture,
#    new Verdict dispatch blocked through admission seam (E owns the real gate)
def test_backend_failure_degrades_with_gap_outbox_and_blocks_new_work(tmp_path: Path) -> None:
    provider = FakeProvider(down=True)
    admission = FakeAdmission()
    outbox = hm.BoundedJsonlOutbox(tmp_path / "o.jsonl", max_events=2)
    bridge = hm.HermesMemoryBridge(provider=provider, outbox=outbox, project="proj-a", admission=admission)

    recall = bridge.handle("pre_llm_call", _fixture("pre_llm_call.json"))
    assert recall.status == "error" and recall.context is None
    assert hm.GAP_BACKEND_UNAVAILABLE in recall.gaps

    for turn in ("t-1", "t-2", "t-3"):
        cap = bridge.handle("post_llm_call", dict(_fixture("post_llm_call.json"), turn_id=turn))
        assert hm.GAP_BACKEND_UNAVAILABLE in cap.gaps
    queued = outbox.pending()
    assert len(queued) == 2  # bounded
    assert [e["identity"]["source_event_id"].split(":")[2] for e in queued] == ["t-2", "t-3"]
    assert outbox.dropped == 1
    assert "SHOULDNOTLEAK" not in (tmp_path / "o.jsonl").read_text(encoding="utf-8")

    dispatched = []
    def dispatch_new_work() -> None:
        if admission.admit_new_work():
            dispatched.append("model-call")
    dispatch_new_work()
    assert dispatched == []
    assert admission.failures and admission.failures[0] == hm.GAP_BACKEND_UNAVAILABLE
