"""BOD80 lane C: Hermes v0.21.3 native plugin memory adapter contract.

Hook surfaces are SOURCE-EVIDENCED (read from installed Hermes source), not yet
observed live. All backends here are fakes; the P client is under security hold.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
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
    first = hm.install(
        hermes_home=home, command=[sys.executable, "-m", "verdict.harness_hermes_memory"]
    )
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

    second = hm.install(
        hermes_home=home, command=[sys.executable, "-m", "verdict.harness_hermes_memory"]
    )
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
    assert yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) == {
        "model": {"default": "x"}
    }


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
    fired = dict(_fixture("pre_llm_call.json"), hook_fired_at=1760655600.0)
    event = hm.translate_hook("pre_llm_call", fired, project="proj-a")
    assert event["schema_version"] == "lifecycle-event/v1"
    assert event["kind"] == "before_first_model_turn"
    ident = event["identity"]
    assert ident["source_harness"] == "hermes"
    assert ident["project"] == "proj-a"
    assert ident["workspace"] == "default"
    assert ident["session_id"] == "20261016_190000_abc123"
    assert ident["source_event_id"] == "hermes:20261016_190000_abc123:t-1:pre_llm_call"
    assert len(event["content_hash"]) == 64
    again = hm.translate_hook("pre_llm_call", fired, project="proj-a")
    assert hm._logical_digest(event) == hm._logical_digest(again)


def test_unsupported_hook_is_explicit() -> None:
    with pytest.raises(ValueError, match="unsupported hermes hook"):
        hm.translate_hook("pre_tool_call", {"session_id": "s"}, project="p")


def test_recall_is_bounded_cited_untrusted_and_empty_is_not_error(tmp_path: Path) -> None:
    provider = FakeProvider()
    bridge = hm.HermesMemoryBridge(
        provider=provider,
        outbox=hm.BoundedJsonlOutbox(tmp_path / "o.jsonl", max_events=5),
        project="proj-a",
        max_context_chars=400,
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
        provider=provider,
        outbox=hm.BoundedJsonlOutbox(tmp_path / "o.jsonl", max_events=5),
        project="proj-a",
    )
    payload = _fixture("post_llm_call.json")
    bridge.handle("post_llm_call", payload)
    bridge.handle("post_llm_call", payload)
    stored = provider.records[("proj-a", "default")]
    assert len(stored) == 1
    assert "SHOULDNOTLEAK" not in json.dumps(stored)
    assert stored[0]["kind"] == "before_yield"
    assert (
        stored[0]["identity"]["source_event_id"]
        == "hermes:20261016_190000_abc123:t-1:post_llm_call"
    )


# 6. two projects/sessions cannot recall each other; payload cannot claim project
def test_cross_project_isolation_and_payload_cannot_claim_project(tmp_path: Path) -> None:
    provider = FakeProvider()
    a = hm.HermesMemoryBridge(
        provider=provider,
        outbox=hm.BoundedJsonlOutbox(tmp_path / "a", max_events=5),
        project="proj-a",
    )
    b = hm.HermesMemoryBridge(
        provider=provider,
        outbox=hm.BoundedJsonlOutbox(tmp_path / "b", max_events=5),
        project="proj-b",
    )
    hostile = dict(_fixture("post_llm_call.json"), project="proj-b", workspace="evil")
    a.handle("post_llm_call", hostile)
    assert ("proj-b", "default") not in provider.records
    assert ("proj-b", "evil") not in provider.records
    assert (
        b.handle("pre_llm_call", dict(_fixture("pre_llm_call.json"), project="proj-a")).status
        == "empty"
    )
    assert provider.queries[-1][:2] == ("proj-b", "default")


# 7. backend failure: harness degrades with named gap, bounded outbox capture,
#    new Verdict dispatch blocked through admission seam (E owns the real gate)
def test_backend_failure_degrades_with_gap_outbox_and_blocks_new_work(tmp_path: Path) -> None:
    provider = FakeProvider(down=True)
    admission = FakeAdmission()
    outbox = hm.BoundedJsonlOutbox(tmp_path / "o.jsonl", max_events=2)
    bridge = hm.HermesMemoryBridge(
        provider=provider, outbox=outbox, project="proj-a", admission=admission
    )

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


# ---- lifecycle-event/v1 backend seam (P-shaped fakes; P client on security hold)


@dataclass(frozen=True)
class Ref:
    provider_id: str
    external_id: str


@dataclass(frozen=True)
class Event:
    raw: dict[str, Any]

    @property
    def content(self) -> str:
        return str(self.raw["content"])

    @property
    def identity(self) -> Any:
        return type("Identity", (), self.raw["identity"])()


@dataclass(frozen=True)
class Hit:
    ref: Ref
    event: Event
    untrusted: bool = True


@dataclass(frozen=True)
class Result:
    status: str
    hits: tuple[Hit, ...] = ()


@dataclass
class Query:
    query: str
    project: str
    workspace: str = "default"
    limit: int = 10


@dataclass
class PShapedFake:
    status: str = "available"
    raise_on_put: bool = False
    raise_on_search: bool = False
    stored: list[Event] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)

    def put(self, event: Event) -> Ref:
        if self.raise_on_put or self.status != "available":
            raise ConnectionError("provider unavailable")
        self.stored.append(event)
        return Ref("fake-verdict-memory", f"ext-{len(self.stored)}")

    def search(self, query: Query) -> Result:
        self.queries.append(query)
        if self.raise_on_search:
            raise TimeoutError("timed out")
        if self.status != "available":
            return Result(status=self.status)
        return Result(
            status="available",
            hits=tuple(Hit(Ref("f", f"ext-{i}"), e) for i, e in enumerate(self.stored)),
        )


class PAdmission:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def record_backend_failure(self, reason: str) -> None:
        self.failures.append(reason)


def _bridge(
    tmp_path: Path,
    fake: PShapedFake,
    loaded: list[bytes] | None = None,
    admission: Any = None,
    max_events: int = 3,
) -> hm.HermesMemoryBridge:
    def load_event(blob: bytes) -> Event:
        if loaded is not None:
            loaded.append(blob)
        return Event(json.loads(blob))

    backend = hm._LifecycleBackendAdapter(fake, load_event=load_event, make_query=Query)
    return hm.HermesMemoryBridge(
        provider=backend,
        outbox=hm.BoundedJsonlOutbox(tmp_path / "outbox.jsonl", max_events=max_events),
        project="proj-a",
        admission=admission,
    )


def test_capture_hands_canonical_lifecycle_event_bytes_to_backend(tmp_path: Path) -> None:
    fake, loaded = PShapedFake(), []
    resp = _bridge(tmp_path, fake, loaded).handle("post_llm_call", _fixture("post_llm_call.json"))
    assert resp.status == "captured" and resp.gaps == ()
    assert len(loaded) == 1 and len(fake.stored) == 1
    record = json.loads(loaded[0])
    assert loaded[0] == hm.canonical_json(record)  # P serialization: sorted, compact, UTF-8
    assert record["schema_version"] == "lifecycle-event/v1"
    assert record["kind"] == "before_yield"
    assert (
        record["identity"]["source_event_id"] == "hermes:20261016_190000_abc123:t-1:post_llm_call"
    )
    assert "SHOULDNOTLEAK" not in loaded[0].decode("utf-8")


def test_recall_maps_available_hits_and_scopes_query_from_config(tmp_path: Path) -> None:
    fake = PShapedFake()
    bridge = _bridge(tmp_path, fake)
    assert bridge.handle("pre_llm_call", _fixture("pre_llm_call.json")).status == "empty"
    bridge.handle("post_llm_call", _fixture("post_llm_call.json"))
    hostile = dict(_fixture("pre_llm_call.json"), project="proj-b", workspace="evil")
    resp = bridge.handle("pre_llm_call", hostile)
    assert resp.status == "ok" and resp.context
    assert "[hermes:20261016_190000_abc123:t-1:post_llm_call]" in resp.context
    assert (fake.queries[-1].project, fake.queries[-1].workspace) == ("proj-a", "default")


def test_non_available_status_is_named_gap_not_empty(tmp_path: Path) -> None:
    for status in ("unavailable", "auth_failed", "malformed", "timeout"):
        admission = PAdmission()
        resp = _bridge(tmp_path, PShapedFake(status=status), admission=admission).handle(
            "pre_llm_call", _fixture("pre_llm_call.json")
        )
        assert resp.status == "error", status
        assert resp.context is None
        assert hm.GAP_BACKEND_UNAVAILABLE in resp.gaps
        assert admission.failures == [hm.GAP_BACKEND_UNAVAILABLE]
    raising = _bridge(tmp_path, PShapedFake(raise_on_search=True)).handle(
        "pre_llm_call", _fixture("pre_llm_call.json")
    )
    assert raising.status == "error" and hm.GAP_BACKEND_UNAVAILABLE in raising.gaps


def test_put_failure_keeps_bounded_outbox_and_reports_gap(tmp_path: Path) -> None:
    fake, admission = PShapedFake(raise_on_put=True), PAdmission()
    bridge = _bridge(tmp_path, fake, admission=admission, max_events=2)
    for turn in ("t-1", "t-2", "t-3"):
        resp = bridge.handle("post_llm_call", dict(_fixture("post_llm_call.json"), turn_id=turn))
        assert resp.status == "queued" and resp.gaps == (hm.GAP_BACKEND_UNAVAILABLE,)
    pending = bridge.outbox.pending()
    assert [r["identity"]["source_event_id"].split(":")[2] for r in pending] == ["t-2", "t-3"]
    assert bridge.outbox.dropped == 1
    assert fake.stored == []
    assert admission.failures == [hm.GAP_BACKEND_UNAVAILABLE] * 3


def test_empty_content_events_are_not_sent_to_backend(tmp_path: Path) -> None:
    fake, loaded = PShapedFake(), []
    bridge = _bridge(tmp_path, fake, loaded)
    assert bridge.handle("on_session_start", {"session_id": "s", "turn_id": ""}).status == "ignored"
    assert (
        bridge.handle(
            "post_llm_call", dict(_fixture("post_llm_call.json"), assistant_response="")
        ).status
        == "ignored"
    )
    assert loaded == [] and fake.stored == []


def test_event_carries_hook_fire_time_as_created_at_for_p_contract() -> None:
    """P LifecycleEvent requires created_at; it must come from the hook fire, not recall time."""
    payload = dict(_fixture("post_llm_call.json"), hook_fired_at=1760655600.25)
    first = hm.translate_hook("post_llm_call", payload, project="proj-a")
    again = hm.translate_hook("post_llm_call", payload, project="proj-a")
    assert first["created_at"] == 1760655600.25
    assert hm._logical_digest(first) == hm._logical_digest(again)
    assert first["provenance"]["created_at_source"] == "hermes_hook_fire"
    missing = hm.translate_hook("post_llm_call", _fixture("post_llm_call.json"), project="proj-a")
    assert isinstance(missing["created_at"], float)
    assert missing["provenance"]["created_at_source"] == "adapter_receipt"


def test_installed_plugin_stamps_hook_fire_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    hm.install(hermes_home=home)
    spec = importlib.util.spec_from_file_location(
        "hermes_verdict_memory_plugin_time", home / "plugins" / hm.PLUGIN_NAME / "__init__.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], *, input: str, **_kw: Any) -> Any:
        seen.update(json.loads(input))
        return type("P", (), {"stdout": "{}"})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.HOOKS["post_llm_call"](session_id="s", turn_id="t", assistant_response="x")
    assert isinstance(seen["hook_fired_at"], float) and seen["hook_fired_at"] > 1.7e9


def test_check_install_refuses_user_owned_plugin_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    user_owned = home / "plugins" / hm.PLUGIN_NAME
    user_owned.mkdir(parents=True)
    with pytest.raises(hm.HermesMemoryCollisionError):
        hm.check_install(hermes_home=home)
    assert sorted(p.name for p in home.rglob("*")) == ["plugins", hm.PLUGIN_NAME]
