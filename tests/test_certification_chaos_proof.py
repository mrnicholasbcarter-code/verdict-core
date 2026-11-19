"""The chaos rehearsal starts from a private copy of the certification proof.

``verdict orchestrate --inject`` deliberately does not read the shared health
cache, so ``scripts/run_certification_rehearsals.py`` must hand the chaos run
its own copy. These tests drive the script's ``run()`` with stubbed children
(no subprocess, no provider calls). Where a path matters, the stub runs the
real ``cli._orchestrate`` and ``build_selector`` on the script's own argv and
environment, so the tests fail if the script and the product ever disagree
about which file the chaos run opens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import conn, row
from verdict import omniroute_health
from verdict.actions.verified_models import StorePaths
from verdict.admission import RuntimeEvidence
from verdict.orchestration import cli
from verdict.orchestration import run as orch_run
from verdict.orchestration.contracts import FailureClassification
from verdict.orchestration.health_cache import HealthCache, ProbeResult, default_cache_path
from verdict.orchestration.session_evidence import SessionLedger, SessionOutcome
from verdict.prove_at_rest_legacy import default_state_path

GATEWAY = "https://cert-gw-7f3a9c.example.invalid:8443"
KEY = "test-key-canary-9d2e"
# Five pools for the chaos scenario (D2: floor is PREFLIGHT_MIN_PLANNER_POOLS=2).
PROVEN = (
    "groq/model-a",
    "nvidia/model-b",
    "cerebras/model-c",
    "mistral/model-d",
    "ollama-cloud/model-e",
)
SCREEN = {
    "mode": "screen",
    "routes_checked": len(PROVEN),
    "passed": len(PROVEN),
    "routes": [
        {
            "route_id": route,
            "result": "pass",
            "capability_tier": 1,
            "supports_tools": True,
            "context_window": 200_000,
        }
        for route in PROVEN
    ],
}
PASSPORTS = b'{"fixture": "passport store bytes"}\n'


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"


def _agentic_passes(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {route for route, entry in HealthCache(path).routes().items() if entry.agentic_ok}


def _cache_a_child_would_use(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    """``default_cache_path()`` as a child started with ``env`` resolves it."""
    with monkeypatch.context() as patch:
        for name in list(os.environ):
            patch.delenv(name)
        for name, value in env.items():
            patch.setenv(name, value)
        return default_cache_path()


def _write_proof(home: Path, cache_path: Path | None = None) -> None:
    """What the preflight leaves: agentic passes in the cache, and a passport store."""
    now = datetime.now(timezone.utc)

    def record(cache: HealthCache) -> None:
        for route in PROVEN:
            cache.record(
                route,
                ProbeResult(
                    category="ok",
                    chat_ok=True,
                    tool_ok=True,
                    probe_class="agentic",
                    agentic_ok=True,
                ),
                now,
            )

    home.mkdir(parents=True, exist_ok=True)
    HealthCache(cache_path or home / "health-cache.json").merge_and_save(record)
    (home / "prove-at-rest").mkdir(exist_ok=True)
    (home / "prove-at-rest" / "state.json").write_bytes(PASSPORTS)


def _ledger_line(route: str, outcome: str) -> SessionOutcome:
    return SessionOutcome(
        route_id=route,
        outcome=outcome,
        kind="real_task",
        failure_class=None if outcome == "pass" else "transport_error",
        verified_by_controller=True,
        at=datetime.now(timezone.utc),
        source="verdict.orchestration.receipt",
    )


def _complete_run(argv: list[str], *, injected: bool) -> Path:
    runs = Path(argv[argv.index("--runs-dir") + 1])
    # The real cli._orchestrate (chaos, in process) has already made its run directory.
    existing = sorted(runs.iterdir()) if runs.is_dir() else []
    run_dir = existing[0] if existing else runs / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts = [{"fault_injected": True, "outcome": "failure"}] if injected else []
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "outcome": "COMPLETE",
                "claimed_outcome": "COMPLETE",
                "producer": {"git_sha": "a" * 40},
                "nodes": [{"attempts": attempts}],
            }
        )
    )
    (run_dir / "events.jsonl").write_text(json.dumps({"gateway_origin": "redacted"}) + "\n")
    return run_dir


def _orchestrate_in_process(
    argv: list[str], env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    """Run the real ``cli._orchestrate`` on ``argv`` under ``env``; no network, no dispatch.

    Returns every health-state path that process resolves, and applies one
    injected-fault write through each product writer.
    """
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv[3:])
    seen: dict[str, Any] = {}

    async def fake_run(goal: str, **kwargs: Any) -> Any:
        selector = kwargs["selector"]
        state_file = Path(args.state_file) if args.state_file else None
        seen["selector_cache"] = selector._health_cache.path.resolve()
        seen["selector_passes"] = _agentic_passes(selector._health_cache.path)
        # Dispatch permits and revocations (runtime.py) use default_cache_path().
        seen["permit_cache"] = default_cache_path()
        seen["refresh_cache"] = StorePaths.defaults(
            state_file.parent if state_file else None
        ).health_cache.resolve()
        seen["passport_store"] = default_state_path()
        # Every SessionLedger() in the chaos process (selector gate, runtime writes).
        seen["session_ledger"] = SessionLedger().path.resolve()
        seen["ladder_state"] = selector._state_path.resolve()
        now = datetime.now(timezone.utc)
        # An injected quota fault: the ladder cools the pool for two hours ...
        selector.record_failure(
            PROVEN[0],
            FailureClassification(
                category="quota_exhausted",
                action="REASSIGN",
                scope="provider",
                evidence="fault-injected:quota",
                cooldown_seconds=7200,
            ),
            now=now,
        )
        # ... and the attempt's dispatch-side cache write lands where permits do.
        HealthCache(default_cache_path()).merge_and_save(
            lambda cache: cache.record(
                PROVEN[0],
                ProbeResult(category="quota_exhausted", chat_ok=False, tool_ok=False),
                now,
            )
        )
        receipt = tmp_path / "in-process-receipt.json"
        receipt.write_text("{}")
        return orch_run.GoldenRunResult(tmp_path, "BLOCKED", "fixture", receipt)

    with monkeypatch.context() as patch:
        for name in list(os.environ):
            patch.delenv(name)
        for name, value in env.items():
            patch.setenv(name, value)
        patch.setattr(orch_run, "fetch_inventory", lambda *a, **k: [row(r) for r in PROVEN])
        patch.setattr(
            orch_run, "fetch_connections", lambda *a, **k: [conn(r.split("/")[0]) for r in PROVEN]
        )
        patch.setattr(
            omniroute_health,
            "load_omniroute_health",
            lambda *a, **k: RuntimeEvidence((), ("omniroute:absent",)),
        )
        patch.setattr(cli, "_executor", lambda args: object())
        patch.setattr(cli, "_planner_executor", lambda args: object())
        patch.setattr(orch_run, "run_golden_path", fake_run)
        cli._orchestrate(args)
    return seen


class _Rehearsal:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts import run_certification_rehearsals as script

        self.script = script
        self.root = tmp_path / "rt"
        self.root.mkdir()
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.shared_home = self.root / "verdict-certification"
        self.shared_cache = self.shared_home / "health-cache.json"
        self.shared_ledger = self.shared_home / "session-evidence.jsonl"
        self.calls: dict[str, dict[str, Any]] = {}
        self.resolved: dict[str, dict[str, Any]] = {}
        self.digests: dict[str, str] = {}
        self.source_at_chaos_start = b""
        self.before_chaos: Any = lambda: None
        self.after_preflight: Any = lambda: None
        monkeypatch.setenv("RUNNER_TEMP", str(self.root))
        monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", GATEWAY)
        monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", KEY)
        monkeypatch.setenv("GITHUB_SHA", "a" * 40)
        monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "snapshot.json")
        monkeypatch.setattr(script, "verify_run_receipt", lambda run_dir: [])
        self.real_run = subprocess.run
        monkeypatch.setattr(script.subprocess, "run", self._child)

    def _child(self, argv: list[str], **kwargs: Any) -> Any:
        env = dict(kwargs.get("env") or {})
        if "identify" in argv:
            # Written where `identify --screen` writes: default_cache_path() under
            # the child's environment. The script must look for its source there.
            written = _cache_a_child_would_use(env, self.monkeypatch)
            assert written == self.shared_cache.resolve()
            _write_proof(Path(env["VERDICT_HOME"]), written)
            # A ledger line already present when the preflight ends.
            SessionLedger(self.shared_ledger).append(_ledger_line(PROVEN[1], "pass"))
            self.digests["after_preflight"] = _digest(self.shared_cache)
            self.digests["ledger_after_preflight"] = _digest(self.shared_ledger)
            self.after_preflight()
            return subprocess.CompletedProcess(argv, 0, json.dumps(SCREEN).encode(), b"")
        if "orchestrate" not in argv:
            return self.real_run(argv, **kwargs)  # git, on the fixture repository
        name = "chaos" if "--inject" in argv else "clean"
        self.calls[name] = {"argv": list(argv), "env": env}
        if name == "clean":
            # The clean run uses the cache the preflight wrote.
            assert _cache_a_child_would_use(env, self.monkeypatch) == self.shared_cache.resolve()
            self.before_chaos()
            self.digests["before_chaos"] = _digest(self.shared_cache)
            self.source_at_chaos_start = (
                self.shared_cache.read_bytes() if self.shared_cache.is_file() else b""
            )
        else:
            state = Path(argv[argv.index("--state-file") + 1]) if "--state-file" in argv else None
            implicit = self.root / ".runs-chaos-chaos" / "chaos" / "health-cache.json"
            seeded = state.parent / "health-cache.json" if state else implicit
            self.digests["seeded_at_chaos_start"] = _digest(seeded)
            ledger = env.get("VERDICT_SESSION_EVIDENCE")
            self.digests["ledger_seeded_at_chaos_start"] = (
                _digest(Path(ledger)) if ledger else "unset"
            )
            self.resolved[name] = _orchestrate_in_process(
                argv, env, self.monkeypatch, self.tmp_path
            )
            self.digests["after_chaos"] = _digest(self.shared_cache)
        _complete_run(argv, injected=name == "chaos")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.fixture
def rehearsal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rehearsal:
    return _Rehearsal(tmp_path, monkeypatch)


def test_chaos_orchestrate_reads_a_cache_holding_the_preflight_passes(
    rehearsal: _Rehearsal,
) -> None:
    """Catches the 2026-11-19 12:09Z failure: chaos BLOCKED at planning because the
    cache its --inject run opens held none of the preflight's agentic passes."""
    rehearsal.script.run()
    chaos = rehearsal.resolved["chaos"]
    # The path comes from the real cli._orchestrate + build_selector, not from a string here.
    assert chaos["selector_passes"] == set(PROVEN)


def test_chaos_resolves_one_private_cache_and_never_writes_the_shared_one(
    rehearsal: _Rehearsal,
) -> None:
    """Catches an injected fault (a two-hour quota cooldown) landing in the cache the
    preflight and the clean run use, through any reader or writer in the chaos process."""
    rehearsal.script.run()
    chaos = rehearsal.resolved["chaos"]
    shared = rehearsal.shared_cache.resolve()
    caches = {chaos["selector_cache"], chaos["permit_cache"], chaos["refresh_cache"]}
    assert shared not in caches
    assert len(caches) == 1  # selector, dispatch permits and refresh agree on one file
    assert chaos["selector_cache"] == chaos["ladder_state"].parent / "health-cache.json"
    assert rehearsal.shared_home.resolve() not in chaos["ladder_state"].parents
    # Deliberately shared: dispatch permits need the passports the preflight and
    # the live confirmations wrote, and an injected terminal never reaches that store.
    assert (
        chaos["passport_store"] == rehearsal.shared_home.resolve() / "prove-at-rest" / "state.json"
    )
    # The simulated injected writes went somewhere, and not into the shared files.
    assert json.loads(chaos["ladder_state"].read_text())["cooldowns"]
    assert rehearsal.digests["after_chaos"] == rehearsal.digests["before_chaos"]
    assert not (rehearsal.shared_home / "orchestration-health.json").exists()
    assert (rehearsal.shared_home / "prove-at-rest" / "state.json").read_bytes() == PASSPORTS


def test_clean_rehearsal_command_and_environment_are_unchanged(rehearsal: _Rehearsal) -> None:
    """Catches the chaos fix leaking into the clean run (state file, cache override, home)."""
    import sys

    from verdict.orchestration.runtime import REDACT_GATEWAY_ORIGIN_ENV

    rehearsal.script.run()
    clean = rehearsal.calls["clean"]
    root = rehearsal.root
    assert clean["argv"] == [
        sys.executable,
        "-m",
        "verdict",
        "orchestrate",
        rehearsal.script.GOALS["clean"],
        "--repo",
        str(root / "fixture-clean"),
        "--runs-dir",
        str(root / "runs-clean"),
        "--gateway",
        GATEWAY,
        "--executor",
        "direct-gateway",
        "--capacity",
        "free",
        "--max-attempts-per-node",
        "4",
        "--max-parallel",
        "3",
        "--attempt-timeout",
        "300",
        "--run-deadline",
        "1500",
        "--json",
    ]
    env = clean["env"]
    assert env["VERDICT_HOME"] == str(root / "verdict-certification")
    assert "VERDICT_HEALTH_CACHE" not in env
    assert "VERDICT_PROVE_AT_REST_STATE" not in env
    assert env[REDACT_GATEWAY_ORIGIN_ENV] == "1"
    assert env["VERDICT_CONNECTIONS_SNAPSHOT"] == str(root / "snapshot.json")
    assert "VERDICT_CERT_CONNECTIONS" not in env
    # The chaos command differs from it only by its private state and the injections.
    chaos_argv = rehearsal.calls["chaos"]["argv"]
    assert chaos_argv[chaos_argv.index("--inject") :] == [
        "--inject",
        "#1=quota",
        "--inject",
        "#2=no_final",
        "--inject",
        "worker#1=rate_limit",
    ]
    assert chaos_argv[chaos_argv.index("--capacity") + 1] == "free"


def _clean_run_observes_a_real_429(rehearsal: _Rehearsal) -> None:
    """What cert-fix8's clean run left behind: a real 429 in the cache and the ledger."""
    HealthCache(rehearsal.shared_cache).merge_and_save(
        lambda cache: cache.record(
            PROVEN[2],
            ProbeResult(category="rate_limited", chat_ok=False, tool_ok=False),
            datetime.now(timezone.utc),
        )
    )
    SessionLedger(rehearsal.shared_ledger).append(_ledger_line(PROVEN[2], "fail"))


def test_chaos_cache_is_the_preflight_cache_not_the_clean_runs(rehearsal: _Rehearsal) -> None:
    """Catches cert-fix8: chaos inheriting the clean run's state. The seed must be the
    shared cache's bytes as the preflight left them (no re-stamp, no extension, no
    injected fault), with nothing the clean run wrote."""
    rehearsal.before_chaos = lambda: _clean_run_observes_a_real_429(rehearsal)
    rehearsal.script.run()
    assert rehearsal.digests["before_chaos"] != rehearsal.digests["after_preflight"]
    assert rehearsal.digests["seeded_at_chaos_start"] == rehearsal.digests["after_preflight"]
    assert "fault-injected" not in rehearsal.shared_cache.read_text()
    # What the chaos selector saw when it started: every preflight pass, no 429.
    assert rehearsal.resolved["chaos"]["selector_passes"] == set(PROVEN)


def test_chaos_ledger_is_the_preflight_ledger_not_the_clean_runs(rehearsal: _Rehearsal) -> None:
    """Catches cert-fix8 13:11Z: the clean run's real 429 in the shared session-evidence
    ledger scored cerebras 0.4 (< 0.5) and gated it out of chaos planning."""
    rehearsal.before_chaos = lambda: _clean_run_observes_a_real_429(rehearsal)
    rehearsal.script.run()
    chaos = rehearsal.resolved["chaos"]
    assert (
        rehearsal.digests["ledger_seeded_at_chaos_start"]
        == (rehearsal.digests["ledger_after_preflight"])
    )
    # The product resolves the private copy, never the shared ledger.
    assert chaos["session_ledger"] != rehearsal.shared_ledger.resolve()
    assert chaos["session_ledger"].parent == chaos["selector_cache"].parent
    seen = {(o.route_id, o.outcome) for o in SessionLedger(chaos["session_ledger"]).load()}
    assert seen == {(PROVEN[1], "pass")}
    # The clean run's write is still in the shared ledger; nothing was edited.
    shared = {(o.route_id, o.outcome) for o in SessionLedger(rehearsal.shared_ledger).load()}
    assert shared == {(PROVEN[1], "pass"), (PROVEN[2], "fail")}


def test_chaos_ledger_seed_is_empty_when_the_preflight_left_none(
    rehearsal: _Rehearsal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a fresh home (no ledger after the preflight) letting chaos read the
    clean run's ledger, or failing to start."""
    monkeypatch.setattr(SessionLedger, "append", lambda self, outcome: None)
    rehearsal.before_chaos = lambda: rehearsal.shared_ledger.write_text(
        json.dumps({"route_id": PROVEN[2], "outcome": "fail"}) + "\n"
    )
    rehearsal.script.run()
    private = rehearsal.resolved["chaos"]["session_ledger"]
    assert private != rehearsal.shared_ledger.resolve()
    assert private.read_bytes() == b""


@pytest.mark.parametrize("damage", ["missing", "unreadable", "no_proof"])
def test_chaos_fails_closed_without_usable_proof(rehearsal: _Rehearsal, damage: str) -> None:
    """Catches chaos being run on an empty cache when the shared proof is gone."""

    def damage_shared_cache() -> None:
        cache = rehearsal.shared_cache
        if damage == "missing":
            cache.unlink()
        elif damage == "unreadable":
            cache.write_text("{not json")
        else:
            cache.write_text(json.dumps({"schema_version": 1, "routes": {}, "buckets": {}}))

    rehearsal.after_preflight = damage_shared_cache
    with pytest.raises(rehearsal.script.RehearsalConfigurationError) as error:
        rehearsal.script.run()
    assert (
        str(error.value)
        == "Chaos rehearsal cannot start: the preflight health cache "
        + {
            "missing": "is missing or unreadable",
            "unreadable": "is not a valid health cache",
            "no_proof": "holds no fresh agentic proof",
        }[damage]
    )
    # The seed is taken before the clean run, so neither run starts.
    assert "chaos" not in rehearsal.calls and "clean" not in rehearsal.calls
    assert not (rehearsal.root / "runs-chaos").exists()
    assert not (rehearsal.root / "attested-rehearsals" / "chaos").exists()
    for value in (GATEWAY, KEY, *PROVEN):
        assert value not in str(error.value)


def test_chaos_private_state_is_outside_the_scanned_and_uploaded_evidence(
    rehearsal: _Rehearsal,
) -> None:
    """Catches the private state (cache, passports) being uploaded unscanned."""
    rehearsal.script.run()
    evidence = rehearsal.root / "attested-rehearsals"
    uploaded = sorted(str(p.relative_to(evidence)) for p in evidence.rglob("*") if p.is_file())
    assert uploaded == [
        "chaos/events.jsonl",
        "chaos/receipt.json",
        "clean/events.jsonl",
        "clean/receipt.json",
    ]
    private = rehearsal.resolved["chaos"]["selector_cache"].parent
    for scanned in (evidence, rehearsal.root / "runs-chaos", rehearsal.root / "runs-clean"):
        assert scanned.resolve() not in (private, *private.parents)
        assert private not in scanned.resolve().parents
