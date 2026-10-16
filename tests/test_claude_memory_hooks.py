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


# --- Installer wiring (verdict.memory_harness.claude) -----------------------


def _all_commands(document: dict) -> list[str]:
    return [
        str(hook.get("command", ""))
        for matchers in document.get("hooks", {}).values()
        for matcher in matchers
        for hook in matcher.get("hooks", [])
    ]


USER_SETTINGS = {
    "theme": "dark",
    "env": {"KEEP": "1"},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user-bash"}]}
        ],
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo user-stop"}]}],
    },
}


def test_hook_command_is_e0_module_entry_point() -> None:
    assert harness_claude.HOOK_COMMAND_TEMPLATE == "python -m verdict.memory_harness.claude {event}"


def test_installed_commands_use_stdin_entry_point_and_no_tmp(tmp_path: Path) -> None:
    from verdict.memory_harness.claude import install_claude_hooks

    settings = tmp_path / "settings.json"
    install_claude_hooks(settings, tmp_path / "unused.db")
    document = json.loads(settings.read_text(encoding="utf-8"))
    commands = _all_commands(document)
    assert commands
    for command in commands:
        assert "/tmp/claude_" not in command
        assert "$(cat" not in command
    for event in harness_claude.CLAUDE_HOOK_EVENT_KINDS:
        expected = harness_claude.HOOK_COMMAND_TEMPLATE.format(event=event)
        assert expected in _all_commands({"hooks": {event: document["hooks"][event]}})


def test_install_twice_is_byte_identical(tmp_path: Path) -> None:
    from verdict.memory_harness.claude import install_claude_hooks

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    install_claude_hooks(settings, tmp_path / "unused.db")
    first = settings.read_bytes()
    install_claude_hooks(settings, tmp_path / "unused.db")
    assert settings.read_bytes() == first


def test_install_preserves_user_hooks_and_keys(tmp_path: Path) -> None:
    from verdict.memory_harness.claude import install_claude_hooks

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    install_claude_hooks(settings, tmp_path / "unused.db")
    document = json.loads(settings.read_text(encoding="utf-8"))
    assert document["theme"] == "dark"
    assert document["env"] == {"KEEP": "1"}
    assert "echo user-bash" in _all_commands({"hooks": {"x": document["hooks"]["PreToolUse"]}})
    assert "echo user-stop" in _all_commands({"hooks": {"x": document["hooks"]["Stop"]}})


def test_install_migrates_legacy_tmp_prompt_hooks(tmp_path: Path) -> None:
    from verdict.memory_harness.claude import install_claude_hooks

    legacy = (
        "verdict hook recall '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"\")' "
        "--json --limit 20 2>/dev/null || true"
    )
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"matcher": "", "hooks": [{"type": "command", "command": legacy}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    install_claude_hooks(settings, tmp_path / "unused.db")
    assert "/tmp/claude_" not in settings.read_text(encoding="utf-8")


def test_uninstall_restores_user_settings_exactly(tmp_path: Path) -> None:
    from verdict.memory_harness.claude import install_claude_hooks, uninstall_claude_hooks

    settings = tmp_path / "settings.json"
    original = json.dumps(USER_SETTINGS, indent=2)
    settings.write_text(original, encoding="utf-8")
    install_claude_hooks(settings, tmp_path / "unused.db")
    assert settings.read_text(encoding="utf-8") != original
    uninstall_claude_hooks(settings)
    assert settings.read_text(encoding="utf-8") == original
    uninstall_claude_hooks(settings)
    assert settings.read_text(encoding="utf-8") == original


def test_uninstall_without_settings_file_is_noop(tmp_path: Path) -> None:
    from verdict.memory_harness.claude import uninstall_claude_hooks

    settings = tmp_path / "absent" / "settings.json"
    uninstall_claude_hooks(settings)
    assert not settings.exists()
    assert not settings.parent.exists()


# --- Hook handler -> outbox --------------------------------------------------


def _fixture_for(project: str, event: str) -> dict:
    return next(p for p in _by_session()[project] if p["hook_event_name"] == event)


def test_hook_handler_writes_outbox_record_from_stdin(
    tmp_path: Path, no_tmp_prompt_reads: list[str]
) -> None:
    payload = _fixture_for("proj-a", "UserPromptSubmit")
    rc = harness_claude.run_hook(
        "UserPromptSubmit",
        json.dumps(payload),
        outbox_dir=tmp_path / "outbox",
        claude_version=CLAUDE_VERSION,
    )
    assert rc == 0
    lines = (tmp_path / "outbox" / harness_claude.OUTBOX_FILENAME).read_text("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [_translate(payload)]
    assert no_tmp_prompt_reads == []


def test_hook_handler_replay_is_idempotent_and_sessions_stay_separate(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    for payload in _load_fixtures() + _load_fixtures():
        harness_claude.run_hook(
            payload["hook_event_name"],
            json.dumps(payload),
            outbox_dir=outbox,
            claude_version=CLAUDE_VERSION,
        )
    records = [
        json.loads(line)
        for line in (outbox / harness_claude.OUTBOX_FILENAME).read_text("utf-8").splitlines()
    ]
    assert len(records) == len(_load_fixtures())
    sessions = {r["identity"]["session_id"]: r["scope"] for r in records}
    assert len(sessions) == 2
    assert set(sessions.values()) == {"proj-a", "proj-b"}


@pytest.mark.parametrize(
    ("event", "stdin_text"),
    [
        ("SessionStart", "not json"),
        ("SessionStart", "[]"),
        ("Notification", json.dumps({"hook_event_name": "Notification"})),
    ],
)
def test_hook_handler_rejects_malformed_or_unknown_input_nonblocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], event: str, stdin_text: str
) -> None:
    rc = harness_claude.run_hook(event, stdin_text, outbox_dir=tmp_path)
    assert rc == harness_claude.HOOK_EXIT_ERROR == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("verdict claude hook: ")
    assert not (tmp_path / harness_claude.OUTBOX_FILENAME).exists()


def test_hook_handler_event_mismatch_is_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _fixture_for("proj-b", "SessionStart")
    rc = harness_claude.run_hook("SessionEnd", json.dumps(payload), outbox_dir=tmp_path)
    assert rc == harness_claude.HOOK_EXIT_ERROR
    assert "mismatch" in capsys.readouterr().err
    assert not (tmp_path / harness_claude.OUTBOX_FILENAME).exists()


def test_hook_handler_unwritable_outbox_is_explicit_and_keeps_existing_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outbox_dir = tmp_path / "outbox"
    first = _fixture_for("proj-a", "SessionStart")
    rc = harness_claude.run_hook(
        "SessionStart", json.dumps(first), outbox_dir=outbox_dir, claude_version=CLAUDE_VERSION
    )
    assert rc == 0
    outbox = outbox_dir / harness_claude.OUTBOX_FILENAME
    before = outbox.read_bytes()
    outbox.chmod(0o444)
    try:
        second = _fixture_for("proj-b", "SessionStart")
        rc = harness_claude.run_hook(
            "SessionStart", json.dumps(second), outbox_dir=outbox_dir, claude_version=CLAUDE_VERSION
        )
    finally:
        outbox.chmod(0o644)
    assert rc == harness_claude.HOOK_EXIT_ERROR
    assert "outbox unwritable" in capsys.readouterr().err
    assert outbox.read_bytes() == before


def test_hook_exit_codes_never_use_blocking_code() -> None:
    # Claude Code treats exit 2 as a blocking error on several events.
    assert harness_claude.HOOK_EXIT_OK == 0
    assert harness_claude.HOOK_EXIT_ERROR not in (0, 2)


def test_module_main_runs_hook_via_subprocess(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    payload = _fixture_for("proj-a", "SessionStart")
    env = {**os.environ, harness_claude.OUTBOX_DIR_ENV: str(tmp_path / "ob")}
    ok = subprocess.run(
        [sys.executable, "-m", "verdict.memory_harness.claude", "SessionStart"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert (ok.returncode, ok.stdout) == (0, ""), ok.stderr
    record = json.loads((tmp_path / "ob" / harness_claude.OUTBOX_FILENAME).read_text("utf-8"))
    assert record["identity"]["session_id"] == payload["session_id"]
    bad = subprocess.run(
        [sys.executable, "-m", "verdict.memory_harness.claude"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert bad.returncode == harness_claude.HOOK_EXIT_ERROR
    assert "usage" in bad.stderr


def test_install_migrates_provisional_harness_claude_hook_entries(tmp_path: Path) -> None:
    from verdict.memory_harness.claude import install_claude_hooks

    settings = tmp_path / "settings.json"
    old = "python -m verdict.harness_claude hook Stop"
    hooks = {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": old}]}]}
    settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    install_claude_hooks(settings, tmp_path / "unused.db")
    commands = _all_commands(json.loads(settings.read_text(encoding="utf-8")))
    assert old not in commands
    assert "python -m verdict.memory_harness.claude Stop" in commands


def test_hook_main_reads_stdin_and_env_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _fixture_for("proj-a", "SessionStart")
    monkeypatch.setenv(harness_claude.OUTBOX_DIR_ENV, str(tmp_path / "ob"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert harness_claude.main(["SessionStart"]) == 0
    record = json.loads((tmp_path / "ob" / harness_claude.OUTBOX_FILENAME).read_text("utf-8"))
    assert record["identity"]["session_id"] == payload["session_id"]
    assert capsys.readouterr().out == ""


# --- Live OmniRoute smoke 2026-10-16 19:23 ET (agy/gemini-3.1-flash-lite; proves hooks, not model) ---
# Separate folder so FIXTURES.glob above never picks these up.

LIVE = Path(__file__).parent / "fixtures" / "claude" / "v2_1_300_omniroute"

SESSIONS = {
    "proj-a": "9c4f5406-5070-472b-81a7-37711d9d6840",
    "proj-b": "2f5b4844-b5b0-46c3-99c3-35341915cad5",
}
LIVE_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
]
TOOL_KINDS = {"PreToolUse": "before_tool", "PostToolUse": "after_tool", "Stop": "before_yield"}


def _live() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(LIVE.glob("*.json"))]


def _ids(p: dict) -> str:
    return f"{Path(p['cwd']).name}-{p['hook_event_name']}-{p.get('tool_name', '')}"


def test_live_capture_is_complete_two_session_set() -> None:
    by: dict[str, list[dict]] = {}
    for p in _live():
        by.setdefault(Path(p["cwd"]).name, []).append(p)
    assert set(by) == set(SESSIONS)
    for proj, payloads in by.items():
        assert [p["hook_event_name"] for p in payloads] == LIVE_EVENTS
        assert {p["session_id"] for p in payloads} == {SESSIONS[proj]}


def test_live_capture_is_anonymized() -> None:
    for path in LIVE.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "/home/nick" not in text and "-home-nick-" not in text, path.name


@pytest.mark.parametrize(
    "payload", [p for p in _live() if p["hook_event_name"] in TOOL_KINDS], ids=_ids
)
def test_live_tool_and_stop_payloads_translate(payload: dict) -> None:
    record = harness_claude.translate_hook_payload(payload, claude_version=CLAUDE_VERSION)
    assert record["kind"] == TOOL_KINDS[payload["hook_event_name"]]
    assert record["identity"]["session_id"] == payload["session_id"]
    assert record["scope"] == Path(payload["cwd"]).name
    assert record["provenance"]["hook_event_name"] == payload["hook_event_name"]


def test_live_records_keep_sessions_isolated() -> None:
    records = [
        harness_claude.translate_hook_payload(p, claude_version=CLAUDE_VERSION) for p in _live()
    ]
    pairs = {(r["identity"]["session_id"], r["scope"]) for r in records}
    assert pairs == {(sid, proj) for proj, sid in SESSIONS.items()}
    ids = [r["identity"]["source_event_id"] for r in records]
    assert len(set(ids)) == len(ids)


def test_live_replay_through_hook_handler_is_idempotent(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    payloads = _live()
    for p in payloads + payloads:
        rc = harness_claude.run_hook(
            p["hook_event_name"], json.dumps(p), outbox_dir=outbox, claude_version=CLAUDE_VERSION
        )
        assert rc == harness_claude.HOOK_EXIT_OK
    lines = (outbox / harness_claude.OUTBOX_FILENAME).read_text("utf-8").splitlines()
    assert len(lines) == len(payloads)
    assert {json.loads(x)["identity"]["session_id"] for x in lines} == set(SESSIONS.values())
