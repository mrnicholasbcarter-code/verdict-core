"""Clean-runner spawn visibility does not supply launch authority."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_orch_eligibility import REQ, conn, row
from verdict.orchestration import cli, run
from verdict.orchestration.eligibility_report import build_selector, prime_visibility
from verdict.subagent_selection import HealthResult


@pytest.mark.parametrize("executor", ["direct-gateway", "prime"])
def test_clean_runner_executor_visibility(tmp_path, monkeypatch, executor):
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VERDICT_HOME", str(state))
    # CP2 removed oauth → SUBSCRIPTION; declare the fake "kr/oauth" connection.
    (state / "connection_billing_overlay.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "provider": "kr",
                        "authType": "oauth",
                        "billing": "subscription",
                        "overage": "hard-stop",
                    }
                ]
            }
        )
    )
    for key in (
        "PRIME_AGENT_CODING_AGENT_DIR",
        "PRIME_AGENT_HOME",
        "PRIME_HOME",
        "VERDICT_ACTIVE_CONTROLLER_ROUTE",
        "VERDICT_HEALTH_CACHE",
    ):
        monkeypatch.delenv(key, raising=False)
    assert not prime_visibility().available
    rows = [
        row("kr/claude-sonnet-ok"),
        row("kr/no-tools", tools=False),
        row("auto/best"),
        row("kr/combo", owned_by="combo"),
    ]
    monkeypatch.setattr(run, "fetch_inventory", lambda *a, **k: rows)
    monkeypatch.setattr(run, "fetch_connections", lambda *a, **k: [conn("kr")])
    monkeypatch.setattr(run, "resolve_api_key", lambda: None)
    calls = []
    monkeypatch.setattr(
        "verdict.subagent_selection.openai_health_probe",
        lambda *a, **k: lambda c: calls.append(c.route_id) or HealthResult(True, ""),
    )
    ladder = build_selector(
        "http://127.0.0.1:1",
        scope="kr/",
        prefer="",
        state_file=tmp_path / "state.json",
        required_capabilities=frozenset({"tools"}),
        capacity=("subscription",),
        visibility_executor=executor,
    )
    assert ladder.admitted.ids == frozenset({"kr/claude-sonnet-ok"})
    assert not ladder.admitted.launchable("kr/claude-sonnet-ok")
    assert not ladder._harness_visible("auto/best")
    assert not ladder._harness_visible("kr/combo")
    chosen, verdicts = ladder.select(REQ, now=datetime.now(timezone.utc))
    if executor == "direct-gateway":
        assert chosen.route_id == "kr/claude-sonnet-ok"
        assert calls == [chosen.route_id]
        assert ladder.admitted.launchable(chosen.route_id)
    else:
        assert chosen is None
        assert calls == []
        assert any(v.reason == "harness_inventory_unavailable" for v in verdicts)
    assert list(home.iterdir()) == []


@pytest.mark.parametrize(
    "executor,expected_source",
    [
        ("prime-headless", "none"),
        ("prime", "none"),  # legacy alias resolves to prime-headless
        ("direct-gateway", "live-gateway"),
    ],
)
def test_visibility_backend_bound_to_executor(executor, expected_source, monkeypatch, tmp_path):
    """Visibility is bound to the chosen worker executor backend via the registry,
    not to the executor_map."""
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    rows = [row("cc/claude-sonnet-5", owned_by="anthropic")]
    monkeypatch.setattr(run, "fetch_inventory", lambda *a, **k: rows)
    monkeypatch.setattr(run, "fetch_connections", lambda *a, **k: [conn("kr")])
    monkeypatch.setattr(run, "resolve_api_key", lambda: None)
    calls = []
    monkeypatch.setattr(
        "verdict.subagent_selection.openai_health_probe",
        lambda *a, **k: lambda c: calls.append(c.route_id) or HealthResult(True, ""),
    )
    ladder = build_selector(
        "http://127.0.0.1:1",
        scope="",
        prefer="",
        state_file=tmp_path / "state.json",
        required_capabilities=frozenset({"tools"}),
        capacity=("subscription",),
        visibility_executor=executor,
    )
    assert ladder._harness_visible.source == expected_source
    if expected_source == "live-gateway":
        assert ladder._harness_visible("cc/claude-sonnet-5") is True
    else:
        assert ladder._harness_visible.ids is None


def test_cli_threads_visibility_policy():
    source = Path(cli.__file__).read_text()
    assert "from verdict.orchestration.harness import DEFAULT_REGISTRY" in source
    assert "visibility_executor=_selector_visibility_executor(args)" in source
    assert "_selector_visibility_executor" in source


def test_selector_visibility_executor_preserves_mixed_map_rule() -> None:
    """A map that sends any node to Prime keeps strict Prime visibility.

    The ladder is shared by every node, so one Prime node means every route
    must be Prime-visible. Fails if the default change (direct-gateway) let a
    mixed Prime run pick routes Prime cannot spawn.
    """
    import argparse

    def ns(executor: str, executor_map: str = "") -> argparse.Namespace:
        return argparse.Namespace(executor=executor, executor_map=executor_map)

    # Pure direct-gateway run -> live-gateway visibility
    assert cli._selector_visibility_executor(ns("direct-gateway")) == "direct-gateway"
    # Pure prime / alias runs -> prime-headless visibility
    assert cli._selector_visibility_executor(ns("prime")) == "prime"
    assert cli._selector_visibility_executor(ns("prime-headless")) == "prime-headless"
    # Any executor map with a Prime backend -> Prime visibility
    assert (
        cli._selector_visibility_executor(ns("direct-gateway", "node-1=prime-headless"))
        == "prime-headless"
    )
    assert (
        cli._selector_visibility_executor(ns("prime-headless", "node-1=direct-gateway"))
        == "prime-headless"
    )
    assert (
        cli._selector_visibility_executor(ns("prime", "node-1=prime,node-2=direct-gateway"))
        == "prime-headless"
    )
    # A map with no Prime backend keeps the --executor visibility
    assert (
        cli._selector_visibility_executor(ns("direct-gateway", "node-1=direct-gateway"))
        == "direct-gateway"
    )


def test_default_visibility_sees_live_routes_missing_from_the_prime_registry(tmp_path, monkeypatch):
    """Fails if plain eligibility (build_selector default) still reads Prime's registry.

    A route new in the live catalog but not yet synced into a (stubbed) Prime
    registry must be visible by default; with Prime selected it stays refused.
    """
    import json

    agent = tmp_path / "prime" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(
        json.dumps({"providers": {"omniroute": {"models": [{"id": "cc/claude-sonnet-5"}]}}})
    )
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(agent))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    rows = [
        row("cc/claude-sonnet-5", owned_by="anthropic"),
        row("cc/claude-new-6", owned_by="anthropic"),
    ]
    monkeypatch.setattr(run, "fetch_inventory", lambda *a, **k: rows)
    monkeypatch.setattr(run, "fetch_connections", lambda *a, **k: [conn("cc")])
    monkeypatch.setattr(run, "resolve_api_key", lambda: None)

    def ladder(**kw):
        return build_selector(
            "http://127.0.0.1:1", scope="", prefer="", state_file=tmp_path / "s.json", **kw
        )

    default = ladder()
    assert default._harness_visible.source == "live-gateway"
    assert default._harness_visible("cc/claude-new-6") is True
    prime = ladder(visibility_executor="prime-headless")
    assert prime._harness_visible.source == "models.json"
    assert prime._harness_visible("cc/claude-sonnet-5") is True
    assert prime._harness_visible("cc/claude-new-6") is False
