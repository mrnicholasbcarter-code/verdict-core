"""Tests for review token-budget scaling (fix/review-token-budget).

Failing-first: these tests are written to FAIL against the original code (fixed
``token_budget=400_000``, no scaling, no env/flag override, no partial guard) and
PASS after the fix.  The partial-review test at the bottom is a regression guard
that also fails before the fix.

No network: every OCR invocation goes through an injected FakeRunner.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    RouteVerdict,
    TaskRequirements,
)
from verdict.orchestration.review import (
    REVIEW_BUDGET_CEILING,
    REVIEW_BUDGET_FLOOR,
    OcrRun,
    OpenCodeReviewer,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class _FakeSelector:
    def __init__(self) -> None:
        self.calls: list[TaskRequirements] = []

    def select(
        self, requirements: TaskRequirements, *, now: object
    ) -> tuple[RouteVerdict, tuple[RouteVerdict, ...]]:
        self.calls.append(requirements)
        route_id = "cx/review-model-5"
        verdict = RouteVerdict(
            route_id=route_id,
            provider=route_id.split("/", 1)[0],
            reached=EligibilityStage.SELECTED,
            failed_stage=None,
            reason="ok",
            capacity_class=CapacityClass.METERED,
            rank=0,
        )
        return verdict, (verdict,)


class _FakeRunner:
    """Captures argv and returns scripted OcrRun results."""

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
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
        argv = list(argv)
        self.calls.append({"argv": argv, "env": dict(env), "timeout": timeout})
        if "--version" in argv:
            return OcrRun(exit_code=0, stdout=self._version_stdout)
        if self._review_payload is not None and "--output" in argv:
            # Inject the model from --model into the manifest so _interpret can
            # validate identity.
            out_path = Path(argv[argv.index("--output") + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            payload_data = json.loads(self._review_payload)
            if "--model" in argv:
                model = argv[argv.index("--model") + 1]
                manifest = payload_data.get("manifest")
                if isinstance(manifest, dict):
                    execution = manifest.get("execution")
                    if isinstance(execution, dict):
                        execution["model"] = model
            out_path.write_text(json.dumps(payload_data))
        return self._review_run


def _build_reviewer(
    tmp_path: Path,
    runner: _FakeRunner,
    *,
    differ_files: list[tuple[str, int]] | None = None,
    **kwargs: Any,
) -> OpenCodeReviewer:
    """Build an OpenCodeReviewer with injected deps.

    ``differ_files`` is a list of (relative_path, size_bytes) tuples.  The
    differ creates files of that exact size under ``tmp_path / "repo"`` so
    ``_changed_file_total_bytes`` can stat them.
    """
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)

    created: list[str] = []
    if differ_files is not None:
        for rel, size in differ_files:
            file_path = repo / rel
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"x" * size)
            created.append(rel)

    def _differ(*, repo: Path, base_ref: str, head_ref: str) -> Sequence[str]:
        return created

    return OpenCodeReviewer(
        _FakeSelector(),
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=runner,
        differ=_differ,
        **kwargs,
    )


def _run(reviewer: OpenCodeReviewer, **overrides: Any):
    kwargs: dict[str, Any] = {
        "repo": Path("/tmp/repo"),
        "base_ref": "main",
        "head_ref": "feature",
        "background": "context",
        "exclude_routes": frozenset(),
        "exclude_families": frozenset(),
    }
    kwargs.update(overrides)
    return asyncio.run(reviewer.review(**kwargs))  # type: ignore[arg-type]


def _budget_from_calls(runner: _FakeRunner) -> int | None:
    """Extract the value passed as --max-tokens-budget from the last review call."""
    for call in reversed(runner.calls):
        argv: list[str] = call["argv"]
        if "--max-tokens-budget" in argv:
            idx = argv.index("--max-tokens-budget")
            return int(argv[idx + 1])
    return None


# ---------------------------------------------------------------------------
# Scaling formula
# ---------------------------------------------------------------------------


def test_budget_scales_with_input_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Budget grows proportionally to changed-file total bytes."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    # 50 KB of changes → (50_000 * 20) = 1_000_000 (above floor, below ceiling)
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[("app.py", 50_000)])
    _run(reviewer, repo=tmp_path / "repo")

    budget = _budget_from_calls(runner)
    assert budget is not None
    assert budget == 1_000_000, f"expected 1_000_000 for 50 KB, got {budget}"


def test_floor_applied_when_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no changed files the floor budget (400 k) is used."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[])
    _run(reviewer, repo=tmp_path / "repo")

    budget = _budget_from_calls(runner)
    assert budget == REVIEW_BUDGET_FLOOR


def test_floor_applied_for_tiny_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files smaller than 20 KB still yield the floor budget."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    # 10 KB → 10_000 * 20 = 200_000 → below floor → floor applies
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[("small.py", 10_000)])
    _run(reviewer, repo=tmp_path / "repo")

    budget = _budget_from_calls(runner)
    assert budget == REVIEW_BUDGET_FLOOR


def test_ceiling_applied_for_large_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Very large diffs are capped at the ceiling (4 M)."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    # 300 KB → 300_000 * 20 = 6_000_000 → above ceiling → ceiling applies
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[("big.py", 300_000)])
    _run(reviewer, repo=tmp_path / "repo")

    budget = _budget_from_calls(runner)
    assert budget == REVIEW_BUDGET_CEILING


# ---------------------------------------------------------------------------
# Explicit override (flag / env paths)
# ---------------------------------------------------------------------------


def test_explicit_budget_overrides_scaling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing token_budget=N bypasses auto-scaling and uses N exactly."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    # With a 50 KB file, auto-scale would give 1 M.
    # An explicit 600 k override must win.
    reviewer = _build_reviewer(
        tmp_path,
        runner,
        differ_files=[("app.py", 50_000)],
        token_budget=600_000,
        token_budget_source="flag",
    )
    _run(reviewer, repo=tmp_path / "repo")

    budget = _budget_from_calls(runner)
    assert budget == 600_000


def test_env_source_recorded_in_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """token_budget_source='env' is propagated to the ReviewResult."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _build_reviewer(
        tmp_path, runner, differ_files=[], token_budget=800_000, token_budget_source="env"
    )
    result = _run(reviewer, repo=tmp_path / "repo")

    assert result.token_budget_used == 800_000
    assert result.token_budget_source == "env"


def test_scaled_source_recorded_in_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-scaled budget is reflected in token_budget_source='scaled'."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[("app.py", 50_000)])
    result = _run(reviewer, repo=tmp_path / "repo")

    assert result.token_budget_used == 1_000_000
    assert result.token_budget_source == "scaled"


def test_default_source_when_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no changed files the source is 'default' (floor applied)."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[])
    result = _run(reviewer, repo=tmp_path / "repo")

    assert result.token_budget_used == REVIEW_BUDGET_FLOOR
    assert result.token_budget_source == "default"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_zero_budget_rejected() -> None:
    """token_budget=0 must raise ValueError at construction."""
    with pytest.raises(ValueError, match="positive"):
        OpenCodeReviewer(
            _FakeSelector(),
            api_key_env="TEST_OCR_KEY",
            out_dir=Path("/tmp/irrelevant"),
            runner=_FakeRunner(OcrRun(exit_code=0)),
            token_budget=0,
        )


def test_negative_budget_rejected() -> None:
    """token_budget=-1 must raise ValueError at construction."""
    with pytest.raises(ValueError, match="positive"):
        OpenCodeReviewer(
            _FakeSelector(),
            api_key_env="TEST_OCR_KEY",
            out_dir=Path("/tmp/irrelevant"),
            runner=_FakeRunner(OcrRun(exit_code=0)),
            token_budget=-1,
        )


def test_above_ceiling_rejected() -> None:
    """token_budget > REVIEW_BUDGET_CEILING must raise ValueError."""
    with pytest.raises(ValueError, match="ceiling"):
        OpenCodeReviewer(
            _FakeSelector(),
            api_key_env="TEST_OCR_KEY",
            out_dir=Path("/tmp/irrelevant"),
            runner=_FakeRunner(OcrRun(exit_code=0)),
            token_budget=REVIEW_BUDGET_CEILING + 1,
        )


# ---------------------------------------------------------------------------
# Partial review is not a pass (regression guard)
# ---------------------------------------------------------------------------


def test_partial_review_is_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial review (budget_exceeded=true, status=partial) must not pass.

    This is a regression guard: before the fix, _empty_review_reason did not
    catch 'status: partial', so a partial review with no blocking findings
    could slip through as PASS.  After the fix it is caught as ERROR.
    """
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-partial.json"))
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[])
    result = _run(reviewer, repo=tmp_path / "repo")

    assert not result.passed, "partial review must not pass"
    assert result.status in {"ERROR", "FAIL"}, (
        f"expected ERROR or FAIL for partial review, got {result.status!r}"
    )
    assert "partial" in result.detail.lower() or "budget" in result.detail.lower(), (
        f"detail should mention 'partial' or 'budget', got {result.detail!r}"
    )


def test_partial_review_budget_in_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The detail string of a partial review includes the tokens consumed."""
    monkeypatch.setenv("TEST_OCR_KEY", "sk-test")
    runner = _FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-partial.json"))
    reviewer = _build_reviewer(tmp_path, runner, differ_files=[])
    result = _run(reviewer, repo=tmp_path / "repo")

    # The fixture has total_tokens=437000 — that number must appear in the detail.
    assert "437000" in result.detail, f"expected total_tokens in detail, got: {result.detail!r}"
