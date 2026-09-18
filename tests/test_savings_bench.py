"""BOD-101 paired legit-task savings bench."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.savings_bench import (
    DEFAULT_SAVINGS_FIXTURE_PATH,
    TALK_TRACK,
    format_savings_report,
    parse_measured_cost,
    run_savings_bench,
)


def test_parse_measured_cost_reads_omniroute_headers_only() -> None:
    cost = parse_measured_cost(
        {
            "headers": {
                "X-OmniRoute-Model": "cx/gpt-5.6-sol",
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


def test_arm_without_used_model_fails_closed(tmp_path: Path) -> None:
    fixture = json.loads(DEFAULT_SAVINGS_FIXTURE_PATH.read_text())
    del fixture["tasks"][0]["direct"]["headers"]["X-OmniRoute-Model"]
    path = tmp_path / "missing-model.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="must specify the model used"):
        run_savings_bench(path)


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
    assert debug["verdict"]["task_class"] == "ordinary"
    assert debug["verdict"]["cache_hit"] is False
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
    assert report["aggregate"]["cache_hit_count"] == 1
    assert (
        report["aggregate"]["claimed_cost_delta_usd"]
        != report["aggregate"]["measured_cost_delta_usd"]
    )


def test_each_arm_specifies_the_model_the_cost_belongs_to() -> None:
    fixture = json.loads(DEFAULT_SAVINGS_FIXTURE_PATH.read_text())
    expected: dict[str, dict[str, str]] = {}
    for task in fixture["tasks"]:
        expected[task["id"]] = {
            "direct": task["direct"]["headers"]["X-OmniRoute-Model"],
            "verdict": task["verdict"]["headers"]["X-OmniRoute-Model"],
        }
        assert expected[task["id"]]["direct"]
        assert expected[task["id"]]["verdict"]
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    encoded = json.dumps(report)
    assert "claude-3-opus" not in encoded
    assert "FREE_IDENTITY" not in encoded
    assert "FRONTIER_IDENTITY" not in encoded
    for task in report["tasks"]:
        wanted = expected[task["task_id"]]
        assert task["direct"]["completed_with"] == wanted["direct"]
        assert task["verdict"]["completed_with"] == wanted["verdict"]
        assert task["verdict"]["routed"]
        assert "opus" not in task["direct"]["completed_with"]
        assert "opus" not in task["verdict"]["completed_with"]
        assert "opus" not in task["verdict"]["routed"]
    rendered = format_savings_report(report)
    assert "direct_used=" in rendered
    assert "verdict_used=" in rendered
    assert "routed=" in rendered
    used_models = {task["verdict"]["completed_with"] for task in report["tasks"]}
    assert len(used_models) > 1


def test_cheaper_model_cache_hit_is_measured_and_not_sold_as_savings() -> None:
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    by_id = {task["task_id"]: task for task in report["tasks"]}
    replay = by_id["debug-null-deref-cache-replay"]
    assert replay["verdict"]["completed_with"]
    assert replay["verdict"]["routed"]
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
    assert f"verdict_used={replay['verdict']['completed_with']}" in rendered


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
    assert "direct_used=" in out
    assert "verdict_used=" in out
    assert "routed=" in out
    assert "cache_hit=true" in out
    assert "claude-3-opus" not in out
    payload = json.loads(output.read_text())
    assert payload["talk_track"] == "we measure"
    assert payload["aggregate"]["task_count"] == 4
    assert payload["aggregate"]["cache_hit_count"] == 1
    for task in payload["tasks"]:
        assert task["direct"]["completed_with"]
        assert task["verdict"]["completed_with"]
        assert task["verdict"]["routed"]
