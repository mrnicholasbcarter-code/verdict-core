"""BOD-101 paired legit-task savings bench."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.savings_bench import (
    DEFAULT_SAVINGS_FIXTURE_PATH,
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
    assert debug["savings_claimed"] is True
    assert debug["deltas"]["cost_usd"] < 0

    refactor = by_id["refactor-with-tests"]
    assert refactor["savings_claimed"] is True
    assert refactor["verdict"]["pack_state"] == "hydrated"

    miss = by_id["implement-from-ac"]
    assert miss["verdict"]["quality"]["passed"] is False
    assert miss["savings_claimed"] is False
    assert miss["withhold_reason"] == "quality_miss"
    assert miss["deltas"]["cost_usd"] < 0
    assert report["aggregate"]["quality_miss_count"] == 1
    assert report["aggregate"]["savings_claimed_count"] == 2
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
    for task in report["tasks"]:
        assert task["direct"]["model"] == FRONTIER_IDENTITY
        assert task["direct"]["completed_with"] == FRONTIER_IDENTITY
        assert task["verdict"]["completed_with"] == task["verdict"]["model"]
        assert task["verdict"]["completed_with"]
        assert "opus" not in task["direct"]["completed_with"]
        assert "opus" not in task["verdict"]["completed_with"]
    rendered = format_savings_report(report)
    assert f"frontier_model: {FRONTIER_IDENTITY}" in rendered
    assert f"direct={FRONTIER_IDENTITY}" in rendered
    assert "verdict=" in rendered


def test_cache_hit_is_not_sold_as_model_savings(tmp_path: Path) -> None:
    fixture = json.loads(DEFAULT_SAVINGS_FIXTURE_PATH.read_text())
    fixture["tasks"] = fixture["tasks"][:2]
    fixture["tasks"][0]["verdict"]["headers"]["X-OmniRoute-Cache-Hit"] = "true"
    fixture["tasks"][0]["verdict"]["quality"] = {"passed": True, "misses": []}
    path = tmp_path / "cache-hit.json"
    path.write_text(json.dumps(fixture))
    report = run_savings_bench(path)
    task = report["tasks"][0]
    assert task["verdict"]["cache_hit"] is True
    assert task["savings_claimed"] is False
    assert task["withhold_reason"] == "cache_hit_is_not_model_savings"


def test_cmd_benchmark_savings_writes_we_measure_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli

    output = tmp_path / "savings.json"
    cli.cmd_benchmark("benchmarks/fixtures/reproducible.json", str(output), savings=True)
    out = capsys.readouterr().out
    assert "talk_track: we measure" in out
    assert "withheld:quality_miss" in out
    assert "direct=cx/gpt-5.6-sol" in out
    assert "verdict=" in out
    assert "claude-3-opus" not in out
    payload = json.loads(output.read_text())
    assert payload["talk_track"] == "we measure"
    assert payload["aggregate"]["task_count"] == 3
