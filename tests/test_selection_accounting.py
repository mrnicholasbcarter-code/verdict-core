"""Selection accounting is evidence, never a second admission authority."""

import pytest

from tests.test_identify_rank import NOW, _conn, _ladder_row, _snapshot
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.subagent_selection import HealthResult


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))


def test_selector_emits_accounting_without_claiming_a_dispatch(tmp_path):
    rows = [
        _ladder_row("free/good", pricing={"input": 0}, tier=0),
        _ladder_row("free/weak", pricing={"input": 0}, tier=3),
        _ladder_row("free/no-tools", pricing={"input": 0}, tier=0),
    ]
    rows[-1]["capabilities"] = {}
    selector = EligibilityLadder(
        rows,
        [_conn("free", auth="apikey", free=True)],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=_snapshot(),
    )
    chosen, verdicts = selector.select(
        TaskRequirements(frontier_worthy=True, max_capability_tier=0), now=NOW
    )
    assert chosen.route_id == "free/good"
    block = chosen.rank_components["selection_accounting"]
    assert block == {
        "eligible_free_attempted": [],
        "known_unavailable": [],
        "known_unavailable_reasons": [],
        "unqualified_supply": ["free/no-tools", "free/weak"],
        "unqualified_reasons": [
            {"route_id": "free/no-tools", "reason": "missing_capability:tools"},
            {"route_id": "free/weak", "reason": "insufficient_capability"},
        ],
        "eligible_unattempted_authority_expired": [],
    }
    assert len(verdicts) == 3
