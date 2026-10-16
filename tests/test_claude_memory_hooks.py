"""RED spec: pure Claude Code hook stdin -> lifecycle-event/v1 translator.

Fixtures are real hook stdin payloads captured from Claude Code 2.1.300 in an
isolated smoke (two sessions, proj-a and proj-b). Record shape is aligned with
packages/verdict-memory lifecycle-event/v1 (contract 0.1.0a1, commit f3d3995c).
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
from pathlib import Path

import pytest

from verdict import harness_claude

FIXTURES = Path(__file__).parent / "fixtures" / "claude" / "v2_1_300"
CLAUDE_VERSION = "2.1.300"
LIFECYCLE_EVENT_SCHEMA = "lifecycle-event/v1"
FORBIDDEN_PROMPT_FILES = ("/tmp/claude_last_prompt", "/tmp/claude_session_prompt")
EXPECTED_KIND = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "before_first_model_turn",
    "SessionEnd": "session_end",
}
V1_REQUIRED_FIELDS = {
    "schema_version",
    "protocol_version",
    "kind",
    "content",
    "identity",
    "scope",
    "provenance",
}
V1_IDENTITY_FIELDS = {"source_harness", "project", "session_id", "source_event_id"}


def _load_fixtures() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.json"))]


def _by_session() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for payload in _load_fixtures():
        out.setdefault(Path(payload["cwd"]).name, []).append(payload)
    return out


def _translate(payload: dict) -> dict:
    return harness_claude.translate_hook_stdin(json.dumps(payload), claude_version=CLAUDE_VERSION)


@pytest.fixture
def no_tmp_prompt_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []
    real_open = builtins.open
    real_read_text = Path.read_text

    def guarded_open(file, *args, **kwargs):
        if str(file) in FORBIDDEN_PROMPT_FILES:
            attempts.append(str(file))
            raise AssertionError(f"translator read forbidden global prompt file {file}")
        return real_open(file, *args, **kwargs)

    def guarded_read_text(self, *args, **kwargs):
        if str(self) in FORBIDDEN_PROMPT_FILES:
            attempts.append(str(self))
            raise AssertionError(f"translator read forbidden global prompt file {self}")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    return attempts


def test_fixtures_are_real_two_session_capture() -> None:
    sessions = _by_session()
    assert set(sessions) == {"proj-a", "proj-b"}
    for payloads in sessions.values():
        assert sorted(p["hook_event_name"] for p in payloads) == sorted(EXPECTED_KIND)
        assert len({p["session_id"] for p in payloads}) == 1
    ids = {payloads[0]["session_id"] for payloads in sessions.values()}
    assert len(ids) == 2


@pytest.mark.parametrize(
    "payload", _load_fixtures(), ids=lambda p: f"{Path(p['cwd']).name}-{p['hook_event_name']}"
)
def test_record_matches_lifecycle_event_v1_shape(
    payload: dict, no_tmp_prompt_reads: list[str]
) -> None:
    record = _translate(payload)
    assert set(record) >= V1_REQUIRED_FIELDS
    assert record["schema_version"] == LIFECYCLE_EVENT_SCHEMA
    assert record["protocol_version"] == "1"
    assert record["kind"] == EXPECTED_KIND[payload["hook_event_name"]]
    assert isinstance(record["content"], str) and record["content"]
    assert set(record["identity"]) >= V1_IDENTITY_FIELDS
    assert no_tmp_prompt_reads == []


@pytest.mark.parametrize(
    "payload", _load_fixtures(), ids=lambda p: f"{Path(p['cwd']).name}-{p['hook_event_name']}"
)
def test_record_carries_session_scope_and_provenance(payload: dict) -> None:
    record = _translate(payload)
    project = Path(payload["cwd"]).name
    assert record["identity"]["source_harness"] == "claude"
    assert record["identity"]["session_id"] == payload["session_id"]
    assert record["identity"]["project"] == project
    assert record["scope"] == project
    assert record["provenance"]["harness"] == "claude"
    assert record["provenance"]["harness_version"] == CLAUDE_VERSION
    assert record["provenance"]["hook_event_name"] == payload["hook_event_name"]
    assert record["provenance"]["cwd"] == payload["cwd"]


def test_event_hash_is_stable_and_distinct() -> None:
    payloads = _load_fixtures()
    first = [_translate(p)["identity"]["source_event_id"] for p in payloads]
    second = [_translate(p)["identity"]["source_event_id"] for p in payloads]
    assert first == second
    assert len(set(first)) == len(payloads)
    for event_id in first:
        assert len(event_id) == 64 and int(event_id, 16) >= 0


def test_event_hash_is_insensitive_to_key_order() -> None:
    payload = _load_fixtures()[0]
    reordered = dict(reversed(list(payload.items())))
    assert (
        _translate(payload)["identity"]["source_event_id"]
        == _translate(reordered)["identity"]["source_event_id"]
    )


def test_prompt_comes_from_stdin_payload(no_tmp_prompt_reads: list[str]) -> None:
    for payloads in _by_session().values():
        ups = next(p for p in payloads if p["hook_event_name"] == "UserPromptSubmit")
        record = _translate(ups)
        assert record["content"] == ups["prompt"]
        assert record["content_hash"] == hashlib.sha256(ups["prompt"].encode("utf-8")).hexdigest()
    assert no_tmp_prompt_reads == []


def test_forbidden_tmp_prompt_files_are_never_read(
    tmp_path: Path, no_tmp_prompt_reads: list[str]
) -> None:
    for payload in _load_fixtures():
        _translate(payload)
    assert no_tmp_prompt_reads == []


def test_two_sessions_never_mix(no_tmp_prompt_reads: list[str]) -> None:
    sessions = _by_session()
    # Interleave the two sessions' events to simulate concurrent hooks.
    interleaved = [
        p for pair in zip(sessions["proj-a"], sessions["proj-b"], strict=True) for p in pair
    ]
    records = [_translate(p) for p in interleaved]
    for payload, record in zip(interleaved, records, strict=True):
        assert record["identity"]["session_id"] == payload["session_id"]
        assert record["scope"] == Path(payload["cwd"]).name
    a_prompt = next(p["prompt"] for p in sessions["proj-a"] if "prompt" in p)
    b_prompt = next(p["prompt"] for p in sessions["proj-b"] if "prompt" in p)
    for record in records:
        if record["scope"] == "proj-a":
            assert b_prompt not in record["content"]
        else:
            assert a_prompt not in record["content"]
    assert no_tmp_prompt_reads == []


def test_translator_is_pure_and_accepts_mapping() -> None:
    payload = _load_fixtures()[1]
    snapshot = json.dumps(payload, sort_keys=True)
    via_mapping = harness_claude.translate_hook_payload(payload, claude_version=CLAUDE_VERSION)
    assert json.dumps(payload, sort_keys=True) == snapshot
    assert via_mapping == _translate(payload)


@pytest.mark.parametrize("bad", ["", "not json", "[]", '{"hook_event_name": "SessionStart"}'])
def test_malformed_stdin_is_rejected(bad: str) -> None:
    with pytest.raises(harness_claude.HarnessClaudeError):
        harness_claude.translate_hook_stdin(bad, claude_version=CLAUDE_VERSION)


def test_records_round_trip_through_fake_provider() -> None:
    """Unit-only: FakeMemoryProvider from verdict-memory P interface (f3d3995c)."""
    verdict_memory = pytest.importorskip("verdict_memory")
    provider = verdict_memory.FakeMemoryProvider()
    for payload in _load_fixtures():
        record = _translate(payload)
        event = verdict_memory.loads_lifecycle_event(json.dumps({**record, "created_at": 0.0}))
        ref = provider.put(event)
        again = provider.put(event)
        assert ref.external_id == again.external_id
        assert event.identity.session_id == payload["session_id"]
