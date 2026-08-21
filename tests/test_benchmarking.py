from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.benchmarking import (
    DEFAULT_COMPARISON_FIXTURE_PATH,
    DEFAULT_FIXTURE_PATH,
    format_benchmark_report,
    load_benchmark_fixture,
    run_comparison_benchmarks,
    run_reproducible_benchmarks,
)


def test_load_fixture_uses_checked_in_reproducible_fixture() -> None:
    fixture = load_benchmark_fixture()
    assert fixture["policy_version"] == "policy-2026-07-13.1"
    assert fixture["settings"]["warmup_iterations"] >= 1
    assert fixture["thresholds"]["contract_roundtrip"]["p95_ns_max"] > 0
    assert fixture["routing_prompts"]


def test_reproducible_benchmark_report_is_deterministic_in_structure(tmp_path: Path) -> None:
    report_a = run_reproducible_benchmarks(DEFAULT_FIXTURE_PATH)
    report_b = run_reproducible_benchmarks(DEFAULT_FIXTURE_PATH)

    comparable_keys = {
        key: report_a[key] for key in report_a if key not in {"generated_at", "benchmarks"}
    }
    comparable_keys_b = {
        key: report_b[key] for key in report_b if key not in {"generated_at", "benchmarks"}
    }
    assert comparable_keys == comparable_keys_b

    benchmark_names_a = [item["name"] for item in report_a["benchmarks"]]
    benchmark_names_b = [item["name"] for item in report_b["benchmarks"]]
    assert (
        benchmark_names_a
        == benchmark_names_b
        == ["contract_roundtrip", "dispatcher_eligibility", "compatibility_routing"]
    )

    output_path = tmp_path / "report.json"
    output_path.write_text(json.dumps(report_a, indent=2, sort_keys=True) + "\n")
    restored = json.loads(output_path.read_text())
    assert restored["fixture_digest_sha256"] == report_a["fixture_digest_sha256"]


def test_report_records_metadata_metrics_and_thresholds() -> None:
    report = run_reproducible_benchmarks(DEFAULT_FIXTURE_PATH)

    assert report["metadata"]["source"] == "checked-in fixture"
    assert report["metadata"]["timer"] == "time.perf_counter_ns"
    assert report["metrics"]["benchmark_count"] == 3
    assert report["metrics"]["sample_count"] == sum(
        item["summary"]["samples"] for item in report["benchmarks"]
    )
    assert report["metrics"]["thresholds_passed"] is True
    assert all(item["thresholds"]["p95_ns_max"] for item in report["benchmarks"])
    assert all(item["threshold_passed"] is True for item in report["benchmarks"])


def test_live_provider_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="explicitly enabled"):
        run_reproducible_benchmarks(DEFAULT_FIXTURE_PATH, live_provider="openai/gpt-4o")


def test_format_report_mentions_local_reproducible_scope() -> None:
    report = run_reproducible_benchmarks(DEFAULT_FIXTURE_PATH)
    text = format_benchmark_report(report)
    assert "mode: local-reproducible" in text
    assert "thresholds_passed: true" in text
    assert "contract_roundtrip" in text
    assert report["notes"][0].startswith("Local reproducible mode")


def test_comparison_benchmark_reports_direct_verdict_regression_and_failover() -> None:
    report = run_comparison_benchmarks(DEFAULT_COMPARISON_FIXTURE_PATH, seed=17)

    assert report["mode"] == "local-comparison"
    assert report["seed"] == 17
    assert [task["task_id"] for task in report["tasks"]] == ["docs-summary", "release-review"]
    assert all(set(task) >= {"task_id", "direct", "verdict", "deltas"} for task in report["tasks"])
    assert report["aggregate"]["verdict_total_cost_usd"] == 1.5
    assert report["regression"]["passed"] is True
    assert report["failover"] == {
        "trigger": "rate_limited",
        "initial_model": "provider-a/model-a",
        "replacement_model": "provider-b/model-b",
        "completed_stages": ["prepare", "execute", "publish"],
        "duplicate_completed_stages": 0,
        "passed": True,
    }


def test_comparison_benchmark_semantic_report_is_seed_deterministic() -> None:
    first = run_comparison_benchmarks(DEFAULT_COMPARISON_FIXTURE_PATH, seed=17)
    second = run_comparison_benchmarks(DEFAULT_COMPARISON_FIXTURE_PATH, seed=17)
    changed = run_comparison_benchmarks(DEFAULT_COMPARISON_FIXTURE_PATH, seed=18)

    assert first == second
    assert first["tasks"] != changed["tasks"]
    assert first["fixture_digest_sha256"] == second["fixture_digest_sha256"]


def test_comparison_benchmark_fails_closed_for_regression(tmp_path: Path) -> None:
    fixture = json.loads(DEFAULT_COMPARISON_FIXTURE_PATH.read_text())
    fixture["baseline"]["verdict_total_cost_usd"] = 0.1
    path = tmp_path / "regression.json"
    path.write_text(json.dumps(fixture))

    report = run_comparison_benchmarks(path, seed=17)

    assert report["regression"]["passed"] is False
    assert report["regression"]["reason"] == "cost_increase_exceeds_budget"


def test_comparison_benchmark_rejects_incomplete_baseline(tmp_path: Path) -> None:
    fixture = json.loads(DEFAULT_COMPARISON_FIXTURE_PATH.read_text())
    del fixture["baseline"]["max_relative_increase"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="max_relative_increase"):
        run_comparison_benchmarks(path, seed=17)
