"""Tests for OpenCodeReviewer + StaticDiffGate OpenCodeReviewer + StaticDiffGate (verdict.orchestration.review).

No network: every ``ocr`` invocation goes through an injected fake runner and
saved sanitized fixtures. Live evidence for the real schema is recorded in the
OpenCodeReviewer + StaticDiffGate delivery notes; the fixtures here are sanitized copies of that output.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    RouteVerdict,
    TaskRequirements,
)
from verdict.orchestration.review import OcrRun, OpenCodeReviewer, StaticDiffGate

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeSelector:
    """ModelSelector stub with a controllable selection and recorded requirements."""

    def __init__(self, chosen: RouteVerdict | None) -> None:
        self._chosen = chosen
        self.requirements: TaskRequirements | None = None

    def evaluate(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict, ...]:
        return () if self._chosen is None else (self._chosen,)

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        self.requirements = requirements
        verdicts = () if self._chosen is None else (self._chosen,)
        return self._chosen, verdicts

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None:
        return None

    def record_success(self, route_id: str, *, now: datetime) -> None:
        return None


def _verdict(route_id: str = "cx/gpt-5.5-low") -> RouteVerdict:
    return RouteVerdict(
        route_id=route_id,
        provider=route_id.split("/", 1)[0],
        reached=EligibilityStage.SELECTED,
        failed_stage=None,
        reason="ok",
        capacity_class=CapacityClass.METERED,
        rank=0,
    )


def _as_ocr_writes(payload: str, argv: Sequence[str]) -> str:
    """Real ``ocr`` records the model it ran with ``--model`` in its manifest."""
    try:
        data = json.loads(payload)
    except ValueError:
        return payload
    manifest = data.get("manifest") if isinstance(data, dict) else None
    execution = manifest.get("execution") if isinstance(manifest, dict) else None
    if isinstance(execution, dict) and "--model" in argv:
        execution["model"] = list(argv)[list(argv).index("--model") + 1]
        return json.dumps(data)
    return payload


class FakeRunner:
    """Records argv/env and returns scripted OcrRun results.

    ``review`` calls write ``payload`` to the ``--output`` path (when given),
    mimicking the real CLI so the interpreter reads a real file.
    """

    def __init__(
        self,
        review_run: OcrRun,
        *,
        review_payload: str | None = None,
        version_stdout: str = "open-code-review v1.12.9 (bccbc15) linux/amd64\n",
    ) -> None:
        self._review_run = review_run
        self._review_payload = review_payload
        self._version_stdout = version_stdout
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
        argv = list(argv)
        self.calls.append({"argv": argv, "env": dict(env), "timeout": timeout})
        if "--version" in argv:
            return OcrRun(exit_code=0, stdout=self._version_stdout)
        if self._review_payload is not None and "--output" in argv:
            out_path = Path(argv[argv.index("--output") + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(_as_ocr_writes(self._review_payload, argv))
        return self._review_run


def _reviewer(
    tmp_path: Path, selector: FakeSelector, runner: FakeRunner, **kwargs: object
) -> OpenCodeReviewer:
    return OpenCodeReviewer(
        selector,
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=runner,
        **kwargs,  # type: ignore[arg-type]
    )


def _run(reviewer: OpenCodeReviewer, **overrides: object):
    kwargs: dict[str, object] = {
        "repo": Path("/tmp/repo"),
        "base_ref": "main",
        "head_ref": "feature",
        "background": "context",
        "exclude_routes": frozenset(),
        "exclude_families": frozenset(),
    }
    kwargs.update(overrides)
    return asyncio.run(reviewer.review(**kwargs))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# OpenCodeReviewer
# --------------------------------------------------------------------------- #


def test_findings_fixture_yields_fail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-findings.json"))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "FAIL"
    assert not result.passed
    assert any(f.severity == "critical" for f in result.findings)
    assert result.findings[0].file == "calc.py"
    assert result.reviewer.startswith("open-code-review v1.12.9")
    assert result.route_id == "cx/gpt-5.5-low"


def test_clean_fixture_yields_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed
    assert result.findings == ()


def test_malformed_output_yields_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload="{not json")
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert not result.passed
    assert "unparseable" in result.detail


def test_missing_output_file_yields_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    # exit 0 but the runner never writes the output file.
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=None)
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert "missing" in result.detail


def test_nonzero_exit_yields_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(
        OcrRun(exit_code=3, stderr="boom"), review_payload=_load_fixture("sample-clean.json")
    )
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert "exited 3" in result.detail


def test_timeout_yields_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=124, timed_out=True))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert "timed out" in result.detail


def test_every_item_failed_yields_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = json.loads(_load_fixture("sample-clean.json"))
    payload["comments"] = []
    payload["manifest"]["coverage"]["failed"] = payload["manifest"]["coverage"]["selected"]
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert "every reviewed item failed" in result.detail


def test_no_eligible_reviewer_yields_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _reviewer(tmp_path, FakeSelector(None), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "no independent reviewer eligible"
    # fail closed: the runner is never invoked when no reviewer is eligible.
    assert runner.calls == []


def test_missing_api_key_yields_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TEST_OCR_KEY", raising=False)
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert "TEST_OCR_KEY" in result.detail


def test_exclusions_forwarded_to_selector(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    selector = FakeSelector(_verdict())
    reviewer = _reviewer(tmp_path, selector, runner)
    _run(
        reviewer,
        exclude_routes=frozenset({"cc/claude-sonnet-5"}),
        exclude_families=frozenset({"claude"}),
    )
    assert selector.requirements is not None
    assert selector.requirements.exclude_families == frozenset({"claude"})
    assert selector.requirements.exclude_routes == frozenset({"cc/claude-sonnet-5"})
    assert selector.requirements.frontier_worthy is True
    assert selector.requirements.reasoning is True
    assert "tools" in selector.requirements.required_capabilities


def test_agy_worker_forwards_antigravity_pool_exclusion(tmp_path: Path, monkeypatch) -> None:
    """Reviewer selection excludes the shared google-antigravity pool."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    selector = FakeSelector(_verdict("kr/gpt-5.6-terra"))
    reviewer = _reviewer(tmp_path, selector, runner)
    _run(
        reviewer, exclude_routes=frozenset({"agy/claude-sonnet-4-6"}), exclude_families=frozenset()
    )
    assert selector.requirements is not None
    assert "google-antigravity" in selector.requirements.exclude_families
    assert selector.requirements.exclude_routes == frozenset({"agy/claude-sonnet-4-6"})


def test_api_key_never_in_argv_or_files(tmp_path: Path, monkeypatch) -> None:
    secret = "sk-super-secret-9999"
    monkeypatch.setenv("TEST_OCR_KEY", secret)
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    # Secret is never present in any recorded argv or env.
    for call in runner.calls:
        assert secret not in " ".join(str(a) for a in call["argv"])  # type: ignore[arg-type]
        assert secret not in json.dumps(call["env"])
    # The isolated HOME (which held the key) is deleted after the run.
    out_dir = tmp_path / "out"
    leftovers = [p for p in out_dir.rglob("*") if p.is_file()]
    for path in leftovers:
        assert secret not in path.read_text(errors="ignore")
    assert not any(p.name.startswith(".ocr-home-") for p in out_dir.iterdir())


def test_severity_high_is_blocking(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = json.loads(_load_fixture("sample-clean.json"))
    payload["comments"] = [
        {
            "path": "x.py",
            "content": "issue",
            "start_line": 4,
            "category": "bug",
            "severity": "major",  # normalizes to high -> blocking -> FAIL
        }
    ]
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "FAIL"
    assert result.findings[0].severity == "high"
    assert result.findings[0].line == 4


def test_low_severity_findings_still_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = json.loads(_load_fixture("sample-clean.json"))
    payload["comments"] = [
        {
            "path": "x.py",
            "content": "nit",
            "start_line": 1,
            "category": "style",
            "severity": "minor",
        }
    ]
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed
    assert result.findings[0].severity == "low"


def test_review_argv_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict("cx/gpt-5.5-low")), runner)
    _run(reviewer)
    review_call = next(c for c in runner.calls if "review" in c["argv"])  # type: ignore[operator]
    argv = review_call["argv"]
    assert argv[:2] == ["ocr", "review"]  # type: ignore[index]
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"  # type: ignore[union-attr]
    assert argv[argv.index("--provider") + 1] == "verdict-omniroute"  # type: ignore[union-attr]
    assert argv[argv.index("--model") + 1] == "cx/gpt-5.5-low"  # type: ignore[union-attr]
    assert argv[argv.index("--audience") + 1] == "agent"  # type: ignore[union-attr]
    # isolated HOME is passed via env, not the user's real HOME.
    assert ".ocr-home-" in review_call["env"]["HOME"]  # type: ignore[operator,index]


# --------------------------------------------------------------------------- #
# StaticDiffGate
# --------------------------------------------------------------------------- #


def _gate(allowed: list[str], changed: list[str]) -> StaticDiffGate:
    def differ(*, repo: Path, base_ref: str, head_ref: str) -> Sequence[str]:
        return changed

    return StaticDiffGate(allowed, differ=differ)


def test_static_gate_pass_within_ownership() -> None:
    gate = _gate(
        ["verdict/orchestration/review.py", "tests/"],
        ["verdict/orchestration/review.py", "tests/test_orch_review.py"],
    )
    result = gate.review(repo=Path("/r"), base_ref="a", head_ref="b")
    assert result.status == "PASS"
    assert result.passed


def test_static_gate_fail_outside_ownership() -> None:
    gate = _gate(
        ["verdict/orchestration/review.py"], ["verdict/orchestration/review.py", "verdict/api.py"]
    )
    result = gate.review(repo=Path("/r"), base_ref="a", head_ref="b")
    assert result.status == "FAIL"
    assert not result.passed
    assert result.findings[0].file == "verdict/api.py"
    assert result.findings[0].severity == "high"


def test_static_gate_error_on_diff_failure() -> None:
    def differ(*, repo: Path, base_ref: str, head_ref: str) -> Sequence[str]:
        raise RuntimeError("git exploded")

    gate = StaticDiffGate(["x"], differ=differ)
    result = gate.review(repo=Path("/r"), base_ref="a", head_ref="b")
    assert result.status == "ERROR"
    assert "could not compute diff" in result.detail


class PoolSelector(FakeSelector):
    """Returns routes in order, honouring exclude_routes, recording failures."""

    def __init__(self, routes: list[str]) -> None:
        super().__init__(None)
        self.routes = routes
        self.failed: list[str] = []

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        self.requirements = requirements
        for route in self.routes:
            if route not in requirements.exclude_routes:
                return _verdict(route), (_verdict(route),)
        return None, ()

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None:
        self.failed.append(route_id)


class SequenceRunner(FakeRunner):
    """First review call fails like a reviewer-provider outage, second is clean."""

    def __init__(self, first: OcrRun, second_payload: str) -> None:
        super().__init__(first)
        self._second_payload = second_payload
        self._reviews = 0

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
        argv = list(argv)
        if "--version" in argv:
            return OcrRun(exit_code=0, stdout="open-code-review v1.12.9\n")
        self._reviews += 1
        self.calls.append({"argv": argv, "env": dict(env), "timeout": timeout})
        if self._reviews == 1:
            return self._review_run
        out_path = Path(argv[argv.index("--output") + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_as_ocr_writes(self._second_payload, argv))
        return OcrRun(exit_code=0)


def test_reviewer_provider_outage_reselects_another_independent_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    clean = (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()
    selector = PoolSelector(["cc/slow-reviewer", "cx/good-reviewer"])
    runner = SequenceRunner(OcrRun(exit_code=1), clean)
    result = _run(_reviewer(tmp_path, selector, runner))
    assert result.status == "PASS"
    assert result.route_id == "cx/good-reviewer"
    assert selector.failed == ["cc/slow-reviewer"]
    models = [c["argv"][c["argv"].index("--model") + 1] for c in runner.calls]
    assert models == ["cc/slow-reviewer", "cx/good-reviewer"]


def test_reviewer_pool_exhaustion_is_error_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    selector = PoolSelector(["cc/a", "cx/b"])
    runner = FakeRunner(OcrRun(exit_code=1))
    result = _run(_reviewer(tmp_path, selector, runner))
    assert result.status == "ERROR" and "exhausted" in result.detail


def test_reviewer_provider_429_cools_the_provider_and_moves_off_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOD-224: a provider-scoped reviewer failure uses the shared failure policy."""
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    clean = (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()
    clean_payload = json.loads(clean)
    clean_payload["manifest"]["execution"]["model"] = "cx/good-reviewer"
    selector = PoolSelector(["cc/a", "cx/good-reviewer"])
    recorded: list[tuple[str, str, str]] = []
    selector.record_failure = lambda route_id, failure, *, now: recorded.append(  # type: ignore[method-assign]
        (route_id, failure.category, failure.scope)
    )
    runner = SequenceRunner(OcrRun(exit_code=1, stderr="429"), json.dumps(clean_payload))
    reviewer = _reviewer(tmp_path, selector, runner)
    reviewer._interpret = _wrap_first_as(reviewer, "ocr review exited 1: HTTP 429 rate limited")  # type: ignore[method-assign]
    result = _run(reviewer)
    assert result.status == "PASS" and result.route_id == "cx/good-reviewer"
    assert recorded == [("cc/a", "rate_limited", "provider")]
    assert [a["route_id"] for a in result.attempts] == ["cc/a", "cx/good-reviewer"]
    assert result.attempts[0]["category"] == "rate_limited"
    assert result.attempts[0]["scope"] == "provider"


def test_reviewer_identity_substitution_fails_closed_and_reselects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review that ran on a model Verdict did not select is not accepted."""
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    clean = json.loads(
        (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()
    )
    substituted = json.loads(json.dumps(clean))
    substituted["manifest"]["execution"]["model"] = "gc/some-default"
    good = json.loads(json.dumps(clean))
    good["manifest"]["execution"]["model"] = "cx/b"
    selector = PoolSelector(["cc/a", "cx/b"])
    runner = PayloadRunner([json.dumps(substituted), json.dumps(good)])
    result = _run(_reviewer(tmp_path, selector, runner))
    assert result.status == "PASS" and result.route_id == "cx/b"
    assert selector.failed == ["cc/a"]
    assert result.attempts[0]["category"] == "model_mismatch"
    assert "identity_mismatch" in str(result.attempts[0]["detail"])


def test_reviewer_identity_mismatch_everywhere_is_error_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    clean = json.loads(
        (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()
    )
    clean["manifest"]["execution"]["model"] = "gc/some-default"
    selector = PoolSelector(["cc/a", "cx/b"])
    runner = PayloadRunner([json.dumps(clean), json.dumps(clean)])
    result = _run(_reviewer(tmp_path, selector, runner))
    assert result.status == "ERROR" and not result.passed
    assert "identity_mismatch" in result.detail and "exhausted" in result.detail
    assert len(result.attempts) == 2


class PayloadRunner(FakeRunner):
    """Each review call writes the next payload and exits 0."""

    def __init__(self, payloads: list[str]) -> None:
        super().__init__(OcrRun(exit_code=0))
        self._payloads = list(payloads)

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
        argv = list(argv)
        if "--version" in argv:
            return OcrRun(exit_code=0, stdout="open-code-review v1.12.9\n")
        self.calls.append({"argv": argv, "env": dict(env), "timeout": timeout})
        out_path = Path(argv[argv.index("--output") + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(self._payloads.pop(0))
        return OcrRun(exit_code=0)


def _wrap_first_as(reviewer: OpenCodeReviewer, detail: str) -> Any:
    original = reviewer._interpret
    calls = {"n": 0}

    def interpret(run: OcrRun, raw_path: Path, route_id: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return reviewer._error(route_id, str(raw_path), detail)
        return original(run, raw_path, route_id)

    return interpret


def test_reviewer_cooled_after_selection_is_never_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOD-224: the reviewer is re-checked immediately before OCR launches."""
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    clean = (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()

    class RevokingSelector(PoolSelector):
        def dispatch_blocker(self, route_id: str, *, now: datetime) -> str | None:
            return "provider:cc" if route_id.startswith("cc/") else None

    selector = RevokingSelector(["cc/a", "cx/b"])
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=clean)
    result = _run(_reviewer(tmp_path, selector, runner))
    assert result.status == "PASS" and result.route_id == "cx/b"
    launched = [
        c["argv"][c["argv"].index("--model") + 1] for c in runner.calls if "--model" in c["argv"]
    ]
    assert launched == ["cx/b"]
    assert result.attempts[0]["status"] == "REVOKED"
    assert result.attempts[0]["category"] == "pre_dispatch_revoked"
    assert all("duration_seconds" in a for a in result.attempts)


def test_idle_reviewer_is_killed_long_before_the_hard_timeout(tmp_path: Path) -> None:
    """BOD-224 AC1: no progress for the idle window ends the run as idle_timed_out."""
    import sys
    import time

    from verdict.orchestration.review import make_subprocess_runner

    runner = make_subprocess_runner(idle_seconds=0.5)
    started = time.monotonic()
    run = runner([sys.executable, "-c", "import time; time.sleep(30)"], env={}, timeout=30)
    assert run.idle_timed_out is True and run.timed_out is False
    assert run.exit_code == 124
    assert time.monotonic() - started < 10


def test_progressing_reviewer_is_not_idle_killed(tmp_path: Path) -> None:
    import sys

    from verdict.orchestration.review import make_subprocess_runner

    script = "import sys, time\nfor i in range(6):\n    print(i, flush=True)\n    time.sleep(0.2)\n"
    run = make_subprocess_runner(idle_seconds=0.6)(
        [sys.executable, "-c", script], env={}, timeout=30
    )
    assert run.idle_timed_out is False and run.timed_out is False
    assert run.exit_code == 0
    assert run.stdout.split() == ["0", "1", "2", "3", "4", "5"]


def test_hard_timeout_still_applies_without_idle_window() -> None:
    import sys

    from verdict.orchestration.review import make_subprocess_runner

    script = "import time\nwhile True:\n    print('x', flush=True)\n    time.sleep(0.05)\n"
    run = make_subprocess_runner(idle_seconds=None)(
        [sys.executable, "-c", script], env={}, timeout=0.5
    )
    assert run.timed_out is True and run.idle_timed_out is False


def test_idle_reviewer_is_replaced_by_another_independent_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    clean = (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()
    selector = PoolSelector(["cc/stuck", "cx/b"])
    runner = SequenceRunner(OcrRun(exit_code=124, idle_timed_out=True), clean)
    result = _run(_reviewer(tmp_path, selector, runner, idle_timeout_seconds=5))
    assert result.status == "PASS" and result.route_id == "cx/b"
    assert selector.failed == ["cc/stuck"]
    assert result.attempts[0]["category"] == "timeout"
    assert "no progress for 5" in str(result.attempts[0]["detail"])


@pytest.mark.parametrize(
    "cause", ["retained", "status", "terminal_state", "selected", "files_reviewed"]
)
def test_skipped_or_empty_review_fails_closed_without_provider_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    """Use the retained skipped OCR shape, and reject each empty signal on its own."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    if cause == "retained":
        proof = (
            Path(__file__).resolve().parents[1]
            / "docs/proof/live-controller-run/review/ocr-raw.json"
        )
        payload = json.loads(proof.read_text())
    else:
        payload = json.loads(_load_fixture("sample-clean.json"))
        if cause == "status":
            payload["status"] = "skipped"
        elif cause == "terminal_state":
            payload["manifest"]["terminal_state"] = "skipped"
        elif cause == "selected":
            payload["manifest"]["coverage"]["selected"] = []
        else:
            payload["summary"]["files_reviewed"] = 0
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert not result.passed
    assert "review skipped" in result.detail
    assert not reviewer._provider_failure(result)
    assert len(result.attempts) == 1
    assert len([c for c in runner.calls if "--output" in c["argv"]]) == 1
    assert "pool exhausted" not in result.detail


# --------------------------------------------------------------------------- #
# BOD-288: Review coverage fail-closed
# --------------------------------------------------------------------------- #


def test_empty_object_fails_closed_with_coverage_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1: {} returns ERROR with exact detail 'review coverage missing'."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload="{}")
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert not result.passed
    assert result.detail == "review coverage missing"


def test_findings_only_object_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 1: {"findings": []} returns ERROR with 'review coverage missing'."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload='{"findings": []}')
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "review coverage missing"


def test_selected_only_signal_is_sufficient_for_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: non-empty selected list alone can produce PASS."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {"manifest": {"coverage": {"selected": [{"path": "x.py"}]}}, "findings": []}
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed


def test_completed_only_signal_is_sufficient_for_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: non-empty completed list alone can produce PASS."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {"manifest": {"coverage": {"completed": [{"path": "y.py"}]}}, "findings": []}
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed


def test_summary_only_signal_is_sufficient_for_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: valid positive files_reviewed alone can produce PASS."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {"summary": {"files_reviewed": 1}, "findings": []}
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed


def test_skipped_status_defeats_positive_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: skipped status takes precedence over positive signals."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {
        "status": "skipped",
        "manifest": {"coverage": {"selected": [{"path": "x.py"}]}},
        "summary": {"files_reviewed": 1},
    }
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "review skipped: no items reviewed"


def test_empty_selected_list_defeats_completed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: selected=[] takes precedence over completed evidence."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {
        "manifest": {"coverage": {"selected": [], "completed": [{"path": "x.py"}]}},
        "summary": {"files_reviewed": 1},
    }
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "review skipped: no items selected"


def test_zero_file_count_defeats_list_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: files_reviewed=0 takes precedence over list evidence."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {
        "manifest": {"coverage": {"selected": [{"path": "x.py"}]}},
        "summary": {"files_reviewed": 0},
    }
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "review skipped: zero files reviewed"


def test_invalid_file_count_defeats_list_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: invalid files_reviewed takes precedence over list evidence."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {
        "manifest": {"coverage": {"selected": [{"path": "x.py"}]}},
        "summary": {"files_reviewed": "not-a-number"},
    }
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "review coverage invalid: files_reviewed is not a count"


def test_coverage_error_does_not_reselect_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 4: missing coverage is not a provider failure, no reselection."""
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    selector = PoolSelector(["cc/first", "cx/second"])
    runner = FakeRunner(OcrRun(exit_code=0), review_payload="{}")
    result = _run(_reviewer(tmp_path, selector, runner))
    assert result.status == "ERROR"
    assert result.detail == "review coverage missing"
    assert len(result.attempts) == 1
    assert result.attempts[0]["route_id"] == "cc/first"
    # Verify no second attempt was made
    assert len([c for c in runner.calls if "--model" in c["argv"]]) == 1
    # Verify no cooldown was recorded
    assert selector.failed == []


def test_retained_dogfood_bod_273_payload_still_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 5: retained one-item complete review from repository proof still passes."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    proof_path = (
        Path(__file__).resolve().parents[1]
        / "docs/proof/dogfood-bod-273-2026-09-28/review/ocr-raw.json"
    )
    payload = json.loads(proof_path.read_text())

    # This payload has:
    # - selected: 1 item
    # - completed: 1 item
    # - files_reviewed: 1
    # - comments: []
    assert len(payload["manifest"]["coverage"]["selected"]) == 1
    assert len(payload["manifest"]["coverage"]["completed"]) == 1
    assert payload["summary"]["files_reviewed"] == 1
    assert payload["comments"] == []

    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    # Use the recorded model to avoid identity mismatch
    recorded_model = payload["manifest"]["execution"]["model"]
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict(recorded_model)), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed
    assert result.findings == ()


# Complete Interview Clean payload (portable hermetic copy per Design § 4)
_INTERVIEW_CLEAN_COMPLETE = r"""
{
  "status": "complete",
  "llm": {
    "provider": "verdict-omniroute",
    "model": "cx/gpt-5.5"
  },
  "message": "Review complete: 0 finding(s) across 2 selected item(s).",
  "summary": {
    "files_reviewed": 2,
    "comments": 0,
    "total_tokens": 15098,
    "input_tokens": 14484,
    "output_tokens": 614,
    "cache_read_tokens": 9728,
    "elapsed": "12s"
  },
  "tool_calls": {
    "total": 2,
    "by_tool": {
      "code_search": 1,
      "file_find": 1
    },
    "failure": 0,
    "failure_by_tool": {},
    "failure_details": []
  },
  "comments": [],
  "groups": [
    {
      "label": "small change set",
      "files": [
        "textkit/slug.py",
        "textkit/words.py"
      ]
    }
  ],
  "session_id": "79e5191e-c7c4-4f4f-b3d8-b0898240dd5a",
  "manifest": {
    "schema_version": "ocr.run-manifest/v1",
    "run_id": "79e5191e-c7c4-4f4f-b3d8-b0898240dd5a",
    "operation": "review",
    "terminal_state": "complete",
    "repository": {},
    "input": {
      "mode": "range",
      "requested_from": "b2491de635f66e873b73ce45f8212ff493a5ceb6",
      "requested_head": "fc0110d1f2ea15473f6edda54efb05cbf384f3d5",
      "resolved_base": "b2491de635f66e873b73ce45f8212ff493a5ceb6",
      "resolved_head": "fc0110d1f2ea15473f6edda54efb05cbf384f3d5",
      "exact_range": "b2491de635f66e873b73ce45f8212ff493a5ceb6..fc0110d1f2ea15473f6edda54efb05cbf384f3d5",
      "source_artifact_sha256": "bdf15b3b6c76fda5df5acc33ed8aab44932aa3f7eb95ef2aae5544aac40392f1"
    },
    "execution": {
      "ocr_version": "v1.12.9",
      "provider": "verdict-omniroute",
      "model": "cx/gpt-5.5",
      "configured_concurrency": 8,
      "rule_config_sha256": "9f33647a25a002db1b7770ce092ffab8b77ef98379e262871a243afd75f798d9",
      "runtime_config_sha256": "f611622b58514c36e4ea02536cab18e15419df06544a048c99b5e3c8e1a21b04"
    },
    "coverage": {
      "selected": [
        {
          "item_id": "1ad1b52fe85fc835f64ed51411d7229297739bcef857208928f70b4e3d58a0f6",
          "path": "textkit/slug.py",
          "fingerprint": "ef572669ea1a3bf8935d0ea18f87b79fcc9b5ea01fc774e34bc5088e08006d65"
        },
        {
          "item_id": "66740eec9663dd9db28078b449e04b21780a7efa05b8785270605e647877d304",
          "path": "textkit/words.py",
          "fingerprint": "61cee5577af9bdacf980475843b53678404690b0858b6c24446bd5ea9587574a"
        }
      ],
      "completed": [
        {
          "item_id": "1ad1b52fe85fc835f64ed51411d7229297739bcef857208928f70b4e3d58a0f6",
          "path": "textkit/slug.py",
          "fingerprint": "ef572669ea1a3bf8935d0ea18f87b79fcc9b5ea01fc774e34bc5088e08006d65"
        },
        {
          "item_id": "66740eec9663dd9db28078b449e04b21780a7efa05b8785270605e647877d304",
          "path": "textkit/words.py",
          "fingerprint": "61cee5577af9bdacf980475843b53678404690b0858b6c24446bd5ea9587574a"
        }
      ],
      "reused": [],
      "failed": [],
      "waived": []
    },
    "elapsed_ms": 12185
  }
}
"""


def test_interview_clean_inline_payload_has_retained_key_structure() -> None:
    """The frozen payload includes the complete OCR envelope and nested manifest."""
    payload = json.loads(_INTERVIEW_CLEAN_COMPLETE)
    assert set(payload) == {
        "status",
        "llm",
        "message",
        "summary",
        "tool_calls",
        "comments",
        "groups",
        "session_id",
        "manifest",
    }
    assert set(payload["manifest"]) == {
        "schema_version",
        "run_id",
        "operation",
        "terminal_state",
        "repository",
        "input",
        "execution",
        "coverage",
        "elapsed_ms",
    }
    assert set(payload["manifest"]["coverage"]) == {
        "selected",
        "completed",
        "reused",
        "failed",
        "waived",
    }
    assert len(payload["groups"]) == 1
    assert payload["groups"][0]["label"] == "small change set"
    assert payload["tool_calls"]["by_tool"].get("file_find") is not None


def test_interview_clean_complete_payload_still_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 5: retained two-item Interview Clean payload still passes (hermetic copy)."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = json.loads(_INTERVIEW_CLEAN_COMPLETE)

    # Verify the hermetic copy has the expected structure
    assert len(payload["manifest"]["coverage"]["selected"]) == 2
    assert len(payload["manifest"]["coverage"]["completed"]) == 2
    assert payload["summary"]["files_reviewed"] == 2
    assert payload["comments"] == []

    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    # Use the recorded model
    recorded_model = payload["manifest"]["execution"]["model"]
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict(recorded_model)), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed
    assert result.findings == ()


def test_missing_coverage_with_blocking_finding_still_errors_on_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage check runs before findings; missing coverage errors first."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {
        "findings": [{"path": "x.py", "severity": "critical", "category": "bug", "content": "bad"}]
    }
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "review coverage missing"


def test_positive_coverage_with_blocking_finding_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 5: blocking findings still reject covered output."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {
        "manifest": {"coverage": {"selected": [{"path": "x.py"}]}},
        "summary": {"files_reviewed": 1},
        "comments": [
            {
                "path": "x.py",
                "severity": "critical",
                "category": "bug",
                "content": "serious issue",
                "start_line": 10,
            }
        ],
    }
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "FAIL"
    assert not result.passed
    assert any(f.severity == "critical" for f in result.findings)


def test_non_list_coverage_fields_do_not_satisfy_list_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: strings, mappings, numbers at selected/completed are not lists."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    for wrong_value in ["string", 123, {"key": "value"}, True, None]:
        payload = {
            "manifest": {"coverage": {"selected": wrong_value, "completed": wrong_value}},
            "findings": [],
        }
        runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
        reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
        result = _run(reviewer)
        assert result.status == "ERROR", f"wrong_value={wrong_value}"
        assert result.detail == "review coverage missing", f"wrong_value={wrong_value}"


@pytest.mark.parametrize("payload", [{"manifest": []}, {"manifest": {"coverage": []}}])
def test_malformed_coverage_containers_fail_closed(
    payload: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "ERROR"
    assert result.detail == "review coverage missing"


def test_summary_alone_can_pass_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: summary-only positive signal works without manifest."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = {"summary": {"files_reviewed": 3}, "findings": []}
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict()), runner)
    result = _run(reviewer)
    assert result.status == "PASS"
    assert result.passed
