"""The certification rehearsal's run evidence never holds the gateway address.

On GitHub that address is a protected secret and the run directory is uploaded,
so ``scripts/run_certification_rehearsals.py`` refuses any run file containing
it. These tests run a real golden path (spy executor, no network, no provider
calls) and apply the same scan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_dagruntime_dispatch_permits import (
    _authority,
    _cache,
    _id,
    _make,
    _orchestrate_args,
    _passport,
)
from tests.test_orch_runtime import NOW, Classifier, Reviewer, Selector, node
from tests.test_orch_runtime import repo as repo
from verdict.orchestration.contracts import RunOutcome, WorkerTerminal, WorkGraph
from verdict.orchestration.executors import DirectGatewayExecutor
from verdict.orchestration.receipt import verify_run_receipt
from verdict.orchestration.runtime import (
    REDACT_GATEWAY_ORIGIN_ENV,
    REDACTED_GATEWAY_ORIGIN,
    DispatchAuthority,
    RuntimePolicy,
)

# Distinctive and non-loopback, so a hit in any file is this value and nothing else.
SECRET_GATEWAY = "https://cert-gw-7f3a9c.example.invalid:8443"


class _WritingSpy(DirectGatewayExecutor):
    """Reports SECRET_GATEWAY as its origin; its send writes ``a.txt`` locally."""

    def __init__(self, base_url: str = SECRET_GATEWAY) -> None:
        super().__init__(base_url=base_url, api_key="fixture-only")
        self.sends: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float, tools: bool = True
    ) -> WorkerTerminal:
        self.sends.append(route_id)
        (cwd / "a.txt").write_text(f"a by {route_id}\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


def _authority_for(*, record: bool, qualified: datetime | None = None) -> DispatchAuthority:
    when = qualified if qualified is not None else datetime.now(timezone.utc)
    base = _authority({"cc/s": _passport("cc/s", qualified=when)})
    return DispatchAuthority(
        gateway_origin=SECRET_GATEWAY, confirm=base.confirm, record_gateway_origin=record
    )


async def _golden(repo: Path, *, record: bool) -> tuple[Path, Any, _WritingSpy]:
    from verdict.orchestration.run import run_golden_path

    ex = _WritingSpy()
    result = await run_golden_path(
        "write a",
        repo=repo,
        runs_root=repo.parent / "runs",
        selector=Selector(["cc/s"]),
        executor=ex,
        classifier=Classifier(),
        reviewer=Reviewer(),
        graph=WorkGraph("g", (node("a"),)),
        run_id="cert1",
        policy=RuntimePolicy(require_dispatch_permit=True, require_review=True),
        dispatch_authority=_authority_for(record=record),
    )
    return result.run_dir, result, ex


def _files_containing(run_dir: Path, needle: str) -> list[str]:
    # The same scan as run_certification_rehearsals.run(): raw bytes of every file.
    return sorted(
        str(f.relative_to(run_dir))
        for f in run_dir.rglob("*")
        if f.is_file() and needle.encode() in f.read_bytes()
    )


def _permit_origins(run_dir: Path) -> list[str]:
    import json

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    return [
        e["data"]["gateway_origin"]
        for e in events
        if e.get("type") == "controller" and e.get("data", {}).get("phase") == "dispatch_permit"
    ]


async def test_redacted_run_writes_the_gateway_address_into_no_evidence_file(repo: Path) -> None:
    """Catches: the rehearsal's evidence leaking the protected gateway address."""
    run_dir, result, ex = await _golden(repo, record=False)
    assert result.outcome == "COMPLETE", result.reason
    assert ex.sends == ["cc/s"]
    origins = _permit_origins(run_dir)
    assert origins == [REDACTED_GATEWAY_ORIGIN]
    assert _files_containing(run_dir, SECRET_GATEWAY) == []
    assert _files_containing(run_dir, "cert-gw-7f3a9c") == []
    # Catches: redaction breaking receipt verification.
    assert verify_run_receipt(run_dir) == []


async def test_default_run_still_records_the_origin_exactly_as_before(repo: Path) -> None:
    """Catches: redaction leaking into the default, digest-bound receipt shape."""
    run_dir, result, _ = await _golden(repo, record=True)
    assert result.outcome == "COMPLETE", result.reason
    assert _permit_origins(run_dir) == ["https://cert-gw-7f3a9c.example.invalid:8443"]
    assert verify_run_receipt(run_dir) == []


@pytest.mark.parametrize("record", [True, False])
async def test_permit_rule_is_unchanged_when_the_origin_is_not_recorded(
    repo: Path, record: bool
) -> None:
    """Catches: redaction relaxing BOD-433 (another gateway's executor must stay denied)."""
    authority = _authority_for(record=record, qualified=NOW)
    ex = _WritingSpy("https://other-gateway.example.invalid")
    rt, ev = _make(repo, ex, ["cc/s"], authority)
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.sends == []
    assert "executor origin mismatch" in result.nodes["a"].reason
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is None

    # Same bound origin: permitted, whichever way it is recorded.
    (repo / "a.txt").unlink(missing_ok=True)
    ok = _WritingSpy()
    rt2, _ = _make(repo, ok, ["cc/s"], authority, run_name="run2")
    result2 = await rt2.run()
    assert result2.outcome is RunOutcome.COMPLETE, result2.reason
    assert ok.sends == ["cc/s"]


@pytest.mark.parametrize(("env", "record"), [(None, True), ("1", False), ("0", True)])
def test_orchestrate_redacts_only_when_asked(
    tmp_path: Path, monkeypatch: Any, env: str | None, record: bool
) -> None:
    """Catches: the CLI ignoring the rehearsal's setting, or redacting by default."""
    from verdict.orchestration import cli, run

    if env is None:
        monkeypatch.delenv(REDACT_GATEWAY_ORIGIN_ENV, raising=False)
    else:
        monkeypatch.setenv(REDACT_GATEWAY_ORIGIN_ENV, env)
    seen: dict[str, Any] = {}
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "fixture-only")
    monkeypatch.setattr(cli, "build_selector", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_executor", lambda args: object())
    monkeypatch.setattr(cli, "_planner_executor", lambda args: object())

    async def fake_run(goal: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        receipt = tmp_path / "receipt.json"
        receipt.write_text("{}")
        return run.GoldenRunResult(tmp_path, "BLOCKED", "fixture", receipt)

    monkeypatch.setattr(run, "run_golden_path", fake_run)
    cli._orchestrate(_orchestrate_args(tmp_path, "--gateway", "http://gw.test:20128/"))
    authority = seen["dispatch_authority"]
    assert authority.gateway_origin == "http://gw.test:20128"
    assert authority.record_gateway_origin is record
