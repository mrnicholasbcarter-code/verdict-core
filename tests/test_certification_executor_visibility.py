"""Clean-runner spawn visibility does not supply launch authority."""

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
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
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
    assert "visibility_executor=args.executor" in source
    assert "_selector_visibility_executor" not in source
