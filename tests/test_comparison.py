"""Tests for the DIRECT-frontier vs Verdict comparison harness (issue #265)."""

from __future__ import annotations

import json
import subprocess
import urllib.request

import pytest

import verdict.discovery
import verdict.intelligence
from verdict.comparison import ComparisonHarness, ComparisonReport
from verdict.gate import Gate
from verdict.models import ModelConfig, ProviderConfig


@pytest.fixture()
def offline_gate(monkeypatch: pytest.MonkeyPatch) -> Gate:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("network/subprocess used in allow_offline mode")

    monkeypatch.setattr(verdict.intelligence, "fetch_models", _boom)
    monkeypatch.setattr(verdict.discovery, "fetch_models", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)

    return Gate(
        primary_model="anthropic/claude-3-opus",
        providers={
            "local": ProviderConfig(
                base_url="http://localhost:20128/v1",
                models={"meta/llama-3.3-70b": ModelConfig(cost_per_1k=0.5)},
            )
        },
        log_path="",
        allow_offline=True,
    )


def test_direct_vs_verdict_deterministic(offline_gate: Gate) -> None:
    """The same task must always produce the identical comparison report."""
    harness = ComparisonHarness(gate=offline_gate)

    first = harness.compare("summarize the readme")
    second = harness.compare("summarize the readme")

    assert isinstance(first, ComparisonReport)
    assert first == second
    assert first.to_json() == second.to_json()


def test_comparison_report_fields(offline_gate: Gate) -> None:
    harness = ComparisonHarness(gate=offline_gate)
    report = harness.compare("summarize the readme")

    assert report.task == "summarize the readme"
    assert report.direct_model == "anthropic/claude-3-opus"
    assert report.verdict_model == "meta/llama-3.3-70b"
    assert report.verdict_strategy == "DIRECT"
    # Verdict routed a cheaper, faster tier than the DIRECT frontier pin.
    assert report.cost_delta < 0
    assert report.latency_delta < 0
    assert 0.0 <= report.quality_score <= 1.0

    payload = json.loads(report.to_json())
    assert set(payload) == {
        "task",
        "direct_model",
        "verdict_strategy",
        "verdict_model",
        "latency_delta",
        "cost_delta",
        "quality_score",
    }


def test_run_direct_and_run_verdict(offline_gate: Gate) -> None:
    harness = ComparisonHarness(gate=offline_gate)

    direct = harness.run_direct("summarize the readme")
    assert direct.model == "anthropic/claude-3-opus"
    assert direct.tier == 0
    assert direct.source == "primary_model"

    verdict_result = harness.run_verdict("summarize the readme")
    assert verdict_result.model == "meta/llama-3.3-70b"
    assert verdict_result.strategy == "DIRECT"
    assert verdict_result.provider == "local"
