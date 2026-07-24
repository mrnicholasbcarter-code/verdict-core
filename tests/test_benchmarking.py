from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.benchmarking import (
    DEFAULT_FIXTURE_PATH,
    format_benchmark_report,
    load_benchmark_fixture,
    run_reproducible_benchmarks,
)


def test_load_fixture_uses_checked_in_reproducible_fixture() -> None:
    fixture = load_benchmark_fixture()
    assert fixture["policy_version"] == "policy-2026-07-13.1"
    assert fixture["settings"]["warmup_iterations"] >= 1
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


def test_live_provider_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="explicitly enabled"):
        run_reproducible_benchmarks(DEFAULT_FIXTURE_PATH, live_provider="openai/gpt-4o")


def test_format_report_mentions_local_reproducible_scope() -> None:
    report = run_reproducible_benchmarks(DEFAULT_FIXTURE_PATH)
    text = format_benchmark_report(report)
    assert "mode: local-reproducible" in text
    assert "contract_roundtrip" in text
    assert report["notes"][0].startswith("Local reproducible mode")
