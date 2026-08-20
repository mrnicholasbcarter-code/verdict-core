"""Unit tests for Gate routing logic."""

from __future__ import annotations

import subprocess
import urllib.request
from datetime import datetime

import pytest

import verdict.discovery
import verdict.intelligence
from verdict.gate import Gate, StrategySelection, strategy_from_decision
from verdict.models import ModelConfig, ProviderConfig


def _offline_gate() -> Gate:
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


def _forbid_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any network discovery or subprocess probe fails the test."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("network/subprocess used in allow_offline mode")

    monkeypatch.setattr(verdict.intelligence, "fetch_models", _boom)
    monkeypatch.setattr(verdict.discovery, "fetch_models", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)


class FakeIntelligence:
    """Stub intelligence that records calls."""

    def __init__(self):
        self.calls = []

    async def rank(self, eligible, task_spec):
        from verdict.intelligence import IntelligenceRanking, RankedCandidate

        self.calls.append((task_spec.prompt, task_spec.criticality, task_spec.context))
        return IntelligenceRanking(
            ranked=[RankedCandidate(model_id="test/model", score=0.9, reasoning="test")],
            task_spec_id=task_spec.prompt[:50],
            profile="test",
        )


def test_critical_is_09():
    assert Gate.COMPLEXITY_MAP["critical"] == 0.9


def test_high_is_07():
    assert Gate.COMPLEXITY_MAP["high"] == 0.7


def test_low_is_02():
    assert Gate.COMPLEXITY_MAP["low"] == 0.2


def test_allow_offline_static_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """allow_offline=True must route from the static catalog with zero I/O."""
    _forbid_io(monkeypatch)

    gate = _offline_gate()
    dec = gate.route("summarize the readme", "medium")

    assert dec.model == "meta/llama-3.3-70b"
    assert dec.provider == "local"
    assert dec.managed_backend_status == "offline"
    assert dec.degraded_mode is False


def test_allow_offline_empty_catalog_falls_back_to_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_io(monkeypatch)

    gate = Gate(
        primary_model="anthropic/claude-3-opus", providers={}, log_path="", allow_offline=True
    )
    dec = gate.route("summarize the readme", "medium")
    assert dec.model == "anthropic/claude-3-opus"


def test_strategy_selection_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """route_with_strategy emits a deterministic StrategySelection record."""
    _forbid_io(monkeypatch)

    gate = _offline_gate()
    dec, sel = gate.route_with_strategy("summarize the readme", "medium")

    assert isinstance(sel, StrategySelection)
    assert sel.strategy in {"DIRECT", "SWARM_AUTODEV"}
    assert sel.strategy == "DIRECT"  # routine tier-2 work executes directly
    assert sel.model == dec.model
    assert sel.reasoning
    # timestamp is ISO-8601; every other field is deterministic
    datetime.fromisoformat(sel.timestamp)

    _, sel_again = gate.route_with_strategy("summarize the readme", "medium")
    assert sel_again.strategy == sel.strategy
    assert sel_again.model == sel.model
    assert sel_again.reasoning == sel.reasoning


def test_strategy_selection_protected_work_is_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_io(monkeypatch)

    gate = _offline_gate()
    dec, sel = gate.route_with_strategy("deploy production infrastructure", "critical")
    assert dec.protected is True
    assert sel.strategy == "SWARM_AUTODEV"
    assert strategy_from_decision(dec).strategy == "SWARM_AUTODEV"
