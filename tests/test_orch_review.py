"""Tests for BOD-185 OpenCodeReviewer + StaticDiffGate (verdict.orchestration.review).

No network: every ``ocr`` invocation goes through an injected fake runner and
saved sanitized fixtures. Live evidence for the real schema is recorded in the
BOD-185 delivery notes; the fixtures here are sanitized copies of that output.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

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
            out_path.write_text(self._review_payload)
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
