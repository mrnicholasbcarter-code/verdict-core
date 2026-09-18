"""BOD-101 paired legit-task savings bench."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.savings_bench import (
    DEFAULT_SAVINGS_FIXTURE_PATH,
    FREE_IDENTITY,
    FRONTIER_IDENTITY,
    TALK_TRACK,
    format_savings_report,
    parse_measured_cost,
    run_savings_bench,
)


def test_parse_measured_cost_reads_omniroute_headers_only() -> None:
    cost = parse_measured_cost(
        {
            "headers": {
                "X-OmniRoute-Response-Cost": "0.012",
                "X-OmniRoute-Tokens-In": "100",
                "X-OmniRoute-Tokens-Out": "20",
                "X-OmniRoute-Cache-Hit": "false",
            }
        }
    )
    assert cost.usd == 0.012
    assert cost.tokens_in == 100
    assert cost.tokens_out == 20
    assert cost.cache_hit is False
    assert cost.source == "headers"


def test_parse_measured_cost_rejects_invented_fields() -> None:
    with pytest.raises(ValueError, match="X-OmniRoute-Response-Cost"):
        parse_measured_cost({"estimated_usd": 0.5, "headers": {}})


def test_savings_bench_measures_three_legit_tasks_and_withholds_quality_miss() -> None:
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    assert report["talk_track"] == TALK_TRACK
    assert report["mode"] == "local-savings"
    kinds = {task["kind"] for task in report["tasks"]}
    assert kinds == {"debug", "refactor_tests", "implement_from_ac"}
    by_id = {task["task_id"]: task for task in report["tasks"]}

    debug = by_id["debug-null-deref"]
    assert debug["verdict"]["pack_state"] == "hydrated"
    assert debug["verdict"]["included_sources"]
    assert debug["verdict"]["cost_source"] == "headers"
    assert debug["direct"]["cost_source"] == "headers"
    assert debug["verdict"]["completed_with"] == FREE_IDENTITY
    assert debug["verdict"]["task_class"] == "ordinary"
    assert debug["verdict"]["cache_hit"] is False
    assert debug["savings_claimed"] is True
    assert debug["deltas"]["cost_usd"] < 0

    refactor = by_id["refactor-with-tests"]
    assert refactor["savings_claimed"] is True
    assert refactor["verdict"]["pack_state"] == "hydrated"
    assert refactor["verdict"]["completed_with"] == FREE_IDENTITY

    miss = by_id["implement-from-ac"]
    assert miss["verdict"]["quality"]["passed"] is False
    assert miss["verdict"]["completed_with"] == FREE_IDENTITY
    assert miss["savings_claimed"] is False
    assert miss["withhold_reason"] == "quality_miss"
    assert miss["deltas"]["cost_usd"] < 0
    assert report["aggregate"]["quality_miss_count"] == 1
    assert report["aggregate"]["savings_claimed_count"] == 2
    assert report["aggregate"]["cache_hit_count"] == 1
    assert (
        report["aggregate"]["claimed_cost_delta_usd"]
        != report["aggregate"]["measured_cost_delta_usd"]
    )


def test_savings_bench_stamps_current_completed_with_identities() -> None:
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    encoded = json.dumps(report)
    assert "claude-3-opus" not in encoded
    assert report["frontier_model"] == FRONTIER_IDENTITY
    assert FRONTIER_IDENTITY == "cx/gpt-5.6-sol"
    assert FREE_IDENTITY == "opencode/hy3-free"
    for task in report["tasks"]:
        assert task["direct"]["model"] == FRONTIER_IDENTITY
        assert task["direct"]["completed_with"] == FRONTIER_IDENTITY
        assert task["verdict"]["completed_with"] == task["verdict"]["model"]
        assert task["verdict"]["completed_with"] == FREE_IDENTITY
        assert "opus" not in task["direct"]["completed_with"]
        assert "opus" not in task["verdict"]["completed_with"]
    rendered = format_savings_report(report)
    assert f"frontier_model: {FRONTIER_IDENTITY}" in rendered
    assert f"direct={FRONTIER_IDENTITY}" in rendered
    assert f"verdict={FREE_IDENTITY}" in rendered


def test_cheaper_model_cache_hit_is_measured_and_not_sold_as_savings() -> None:
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    by_id = {task["task_id"]: task for task in report["tasks"]}
    replay = by_id["debug-null-deref-cache-replay"]
    assert replay["verdict"]["completed_with"] == FREE_IDENTITY
    assert replay["verdict"]["task_class"] == "ordinary"
    assert replay["verdict"]["cache_hit"] is True
    assert replay["direct"]["cache_hit"] is False
    assert replay["verdict"]["quality"]["passed"] is True
    assert replay["deltas"]["cost_usd"] < 0
    assert replay["savings_claimed"] is False
    assert replay["withhold_reason"] == "cache_hit_is_not_model_savings"
    rendered = format_savings_report(report)
    assert "cache_hits: 1" in rendered
    assert "cache_hit=true" in rendered
    assert "withheld:cache_hit_is_not_model_savings" in rendered


def test_cmd_benchmark_savings_writes_we_measure_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli

    output = tmp_path / "savings.json"
    cli.cmd_benchmark("benchmarks/fixtures/reproducible.json", str(output), savings=True)
    out = capsys.readouterr().out
    assert "talk_track: we measure" in out
    assert "withheld:quality_miss" in out
    assert "withheld:cache_hit_is_not_model_savings" in out
    assert "direct=cx/gpt-5.6-sol" in out
    assert "verdict=opencode/hy3-free" in out
    assert "cache_hit=true" in out
    assert "claude-3-opus" not in out
    payload = json.loads(output.read_text())
    assert payload["talk_track"] == "we measure"
    assert payload["aggregate"]["task_count"] == 4
    assert payload["aggregate"]["cache_hit_count"] == 1
