"""BOD-271: one mission, three roles, three independent assignments.

The controller, an implementation worker and the independent reviewer are
assigned separately from the SAME live inventory, each from its own role
requirements:

* controller: mission burden -> ``mission_capability_floor`` -> controller
  selection (admission-narrowed seeds, BOD-104 optimizer);
* worker: bounded low-risk implement node -> ``EligibilityLadder``;
* reviewer: review node, implementer route and family excluded ->
  ``EligibilityLadder``.

No role inherits another role's model.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.test_controller_launch import NOW as CTRL_NOW
from tests.test_controller_launch import _ctrl_offer, _ctrl_route, _hooks, _mission
from tests.test_orch_eligibility import NOW, conn, make_ladder, row
from verdict.controller_selection import select_controller_launch
from verdict.orchestration.contracts import NodeKind, TaskRequirements, WorkNode, route_family

INVENTORY = (
    "kr/claude-opus-5.5",  # tier 0
    "kr/claude-sonnet-5",  # tier 1
    "cx/gpt-6-sol",  # tier 0, different family
    "kr/claude-haiku-4.5",  # tier 3
)


def _worker_and_reviewer(tmp_path: Path) -> tuple[str, str]:
    rows = [row(route_id, owned_by=route_id.split("/", 1)[0]) for route_id in INVENTORY]
    connections = [conn("kr"), conn("cx")]
    implement = WorkNode(
        "impl", "rename a helper", owned_files=("x.py",), verification_command=("true",), risk="low"
    )
    ladder, _ = make_ladder(tmp_path / "worker", rows, connections)
    worker, _ = ladder.select(
        replace(TaskRequirements.for_node(implement), frontier_worthy=True), now=NOW
    )
    assert worker is not None
    review = WorkNode("review", "independent review", kind=NodeKind.REVIEW)
    reviewer_req = TaskRequirements.for_node(
        review,
        exclude_routes=frozenset({worker.route_id}),
        exclude_families=frozenset({route_family(worker.route_id)}),
    )
    ladder, _ = make_ladder(tmp_path / "reviewer", rows, connections)
    reviewer, _ = ladder.select(reviewer_req, now=NOW)
    assert reviewer is not None
    return worker.route_id, reviewer.route_id


def _controller(burden: str | None) -> str:
    offers = [
        _ctrl_offer(
            _ctrl_route(f"omniroute/{route_id}", provider="omniroute", model=route_id),
            ctx_digest=f"ctx-{route_id}",
            execution_tokens=1_000 if "haiku" in route_id else 20_000,
        )
        for route_id in INVENTORY
    ]
    hooks, _, _ = _hooks(seed=offers)
    mission = replace(_mission(), orchestration_burden=burden)
    decision = select_controller_launch(mission, hooks=hooks, now=CTRL_NOW)
    return decision.prime_target.prime_model


def test_controller_worker_and_reviewer_are_assigned_independently(tmp_path: Path) -> None:
    controller = _controller("high")
    worker, reviewer = _worker_and_reviewer(tmp_path)

    # Bounded implementation takes the cheapest sufficient model.
    assert worker == "kr/claude-haiku-4.5"
    # High-burden control needs a strong model; the small one never qualifies.
    assert controller != "kr/claude-haiku-4.5"
    assert controller in {"kr/claude-opus-5.5", "kr/claude-sonnet-5", "cx/gpt-6-sol"}
    # The reviewer is strong, independent of the implementer route and family.
    assert reviewer != worker
    assert route_family(reviewer) != route_family(worker)
    assert reviewer in {"cx/gpt-6-sol"}


def test_controller_floor_follows_the_mission_not_the_worker(tmp_path: Path) -> None:
    # Same inventory, bounded mission: the controller may be the cheap model,
    # while the reviewer still has to be strong and independent.
    controller = _controller(None)
    _, reviewer = _worker_and_reviewer(tmp_path)
    assert controller == "kr/claude-haiku-4.5"
    assert reviewer == "cx/gpt-6-sol"
