"""Offline tests for explicit CI rehearsal runtime bounds."""

import argparse

import pytest

from verdict.orchestration import cli, eligibility_report, run
from verdict.orchestration.eligibility import HarnessVisibility
from verdict.subagent_selection import HealthResult


def _parser():
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    return parser


def test_optional_bounds_preserve_defaults():
    args = _parser().parse_args(["orchestrate", "goal"])
    assert args.capacity == ()
    assert args.max_attempts_per_node == 4
    args = _parser().parse_args(
        ["orchestrate", "goal", "--capacity", "free,subscription", "--max-attempts-per-node", "2"]
    )
    assert args.capacity == ("free", "subscription")
    assert args.max_attempts_per_node == 2


@pytest.mark.parametrize(
    "option,value", [("--capacity", ""), ("--capacity", "paid"), ("--max-attempts-per-node", "0")]
)
def test_invalid_bounds_rejected(option, value):
    with pytest.raises(SystemExit):
        _parser().parse_args(["orchestrate", "goal", option, value])


@pytest.mark.parametrize("capacity", [(), ("free", "subscription")])
def test_capacity_is_canonical_narrowing_before_probe(tmp_path, monkeypatch, capacity):
    rows = [
        {"id": "free/model", "owned_by": "free", "pricing": {"input": 0}},
        {"id": "sub/model", "owned_by": "sub", "pricing": {"input": 1}},
        {"id": "paid/model", "owned_by": "paid", "pricing": {"input": 1}},
        {"id": "unknown/model", "owned_by": "unknown"},
    ]
    connections = [
        {"provider": name, "isActive": True, "authType": auth}
        for name, auth in [
            ("free", "apikey"),
            ("sub", "oauth"),
            ("paid", "apikey"),
            ("unknown", "apikey"),
        ]
    ]
    monkeypatch.setattr(run, "fetch_inventory", lambda *a, **k: rows)
    monkeypatch.setattr(run, "fetch_connections", lambda *a, **k: connections)
    monkeypatch.setattr(
        eligibility_report,
        "prime_visibility",
        lambda **k: HarnessVisibility(frozenset(r["id"] for r in rows), source="test"),
    )
    probes = []
    monkeypatch.setattr(
        "verdict.subagent_selection.openai_health_probe",
        lambda *a, **k: lambda c: probes.append(c) or HealthResult(True, ""),
    )
    selector = eligibility_report.build_selector(
        "http://127.0.0.1:20128",
        scope="",
        prefer="",
        state_file=tmp_path / "health.json",
        capacity=capacity,
    )
    assert probes == []
    assert selector.admitted is not None
    if capacity:
        assert selector.admitted.ids == {"free/model", "sub/model"}
        for route in ("paid/model", "unknown/model"):
            assert selector.admitted.first_failure(route).reason == "outside_capacity_filter"
            assert not selector.admitted.launchable(route)
    else:
        assert selector.admitted.ids == {r["id"] for r in rows}
