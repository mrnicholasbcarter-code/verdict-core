"""Tests for bounded, advisory-only RuVector negotiation."""

from __future__ import annotations

from verdict.ruvector_adapter import ProbeResult, ReadinessStatus, RuVectorAdapter


def _runner(results: list[ProbeResult]):
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], timeout: float, limit: int) -> ProbeResult:
        calls.append(argv)
        assert timeout == 0.5
        assert limit == 128
        return results[len(calls) - 1]

    return run, calls


def test_negotiation_is_ready_only_when_version_and_capabilities_are_complete() -> None:
    runner, calls = _runner(
        [
            ProbeResult(0, "ruvector 0.2.25\n", ""),
            ProbeResult(0, '{"commands":["search","trajectory"]}', ""),
        ]
    )
    adapter = RuVectorAdapter(timeout_ms=500, max_output_bytes=128, runner=runner)

    report = adapter.negotiate(("search",))

    assert report.status is ReadinessStatus.READY
    assert report.advisory_retrieval_enabled is True
    assert adapter.can_use("search", report) is True
    assert adapter.can_use("delete", report) is False
    assert calls == [("ruvector", "--version"), ("ruvector", "capabilities", "--json")]
    assert report.digest == report.digest


def test_missing_or_unsupported_capabilities_degrade_without_enabling_retrieval() -> None:
    runner, _ = _runner(
        [ProbeResult(0, "ruvector 0.2.25\n", ""), ProbeResult(1, "", "unknown command")]
    )
    report = RuVectorAdapter(timeout_ms=500, max_output_bytes=128, runner=runner).negotiate(
        ("search",)
    )

    assert report.status is ReadinessStatus.DEGRADED
    assert report.advisory_retrieval_enabled is False
    assert "capability negotiation unsupported" in report.limitations


def test_timeout_and_truncation_are_explicit_and_fail_closed() -> None:
    runner, _ = _runner(
        [
            ProbeResult(-1, "", "timeout", timed_out=True),
            ProbeResult(0, "{}", "", output_truncated=True),
        ]
    )
    report = RuVectorAdapter(timeout_ms=500, max_output_bytes=128, runner=runner).negotiate()

    assert report.status is ReadinessStatus.UNAVAILABLE
    assert report.advisory_retrieval_enabled is False
    assert "version probe timed out" in report.limitations
    assert "capability probe output truncated" in report.limitations


def test_argv_boundary_does_not_accept_shell_syntax_in_executable() -> None:
    try:
        RuVectorAdapter(executable="ruvector; rm -rf")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe executable accepted")
