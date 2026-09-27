#!/usr/bin/env python3
"""PR selection report: which model Verdict's real ladder assigns per role (offline).

Runs the production ``EligibilityLadder`` and ``TaskRequirements.for_node``
over a checked-in fixture inventory (no network, no live provider). For each
role/task class it reports the selected route, its capability tier, and why
the other routes were dropped. It fails (exit 1) if any selection violates the
role's capability floor, which is the regression this report exists to catch.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from verdict.classifier import classify_known
from verdict.orchestration.contracts import NodeKind, TaskRequirements, WorkNode, route_family
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.subagent_selection import HealthResult

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent.parent / "benchmarks/fixtures/selection_inventory.json"
)
IMPLEMENTER_FOR_REVIEW = "kr/claude-sonnet-5"


def _node(node_id: str, **kw: Any) -> WorkNode:
    base: dict[str, Any] = {"owned_files": ("x.py",), "verification_command": ("true",)}
    if kw.get("kind") is NodeKind.REVIEW:
        base = {}
    return WorkNode(node_id, node_id, **{**base, **kw})


ROLES: tuple[tuple[str, WorkNode, dict[str, Any]], ...] = (
    ("bounded implementation (low risk)", _node("low", risk="low"), {}),
    ("reasoning / medium risk", _node("medium", risk="medium", reasoning=True), {}),
    ("high-risk implementation", _node("high", risk="high"), {}),
    (
        f"independent review (implementer {IMPLEMENTER_FOR_REVIEW})",
        _node("review", kind=NodeKind.REVIEW),
        {
            "exclude_routes": frozenset({IMPLEMENTER_FOR_REVIEW}),
            "exclude_families": frozenset({route_family(IMPLEMENTER_FOR_REVIEW)}),
        },
    ),
)


def _rows(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in fixture["inventory"]:
        context = int(item["context_length"])
        rows.append(
            {
                **item,
                "max_input_tokens": context,
                "max_output_tokens": 32_000,
                "capabilities": {"tool_calling": True, "reasoning": True},
            }
        )
    return rows


def run(fixture_path: Path) -> dict[str, Any]:
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    rows = _rows(fixture)
    prices = {r["id"]: sum(float(v) for v in r["pricing"].values()) for r in rows}
    results: list[dict[str, Any]] = []
    violations: list[str] = []
    for label, node, overrides in ROLES:
        requirements = TaskRequirements.for_node(node, **overrides)
        with tempfile.TemporaryDirectory() as tmp:
            ladder = EligibilityLadder(
                rows,
                fixture["connections"],
                lambda _route: HealthResult(healthy=True, category=""),
                Path(tmp) / "state.json",
            )
            chosen, verdicts = ladder.select(requirements, now=_now())
        dropped = collections.Counter(v.reason for v in verdicts if v.failed_stage is not None)
        selected = chosen.route_id if chosen else None
        tier = classify_known(selected) if selected else None
        if selected is None:
            violations.append(f"{label}: no route selected")
        elif tier is None or tier > requirements.max_capability_tier:
            violations.append(
                f"{label}: {selected} (tier {tier}) exceeds floor {requirements.max_capability_tier}"
            )
        results.append(
            {
                "role": label,
                "floor": requirements.max_capability_tier,
                "selected": selected,
                "tier": tier,
                "capacity": chosen.capacity_class.value if chosen else None,
                "price_per_mtok": prices.get(selected or ""),
                "dropped": dict(sorted(dropped.items())),
            }
        )
    return {
        "fixture": str(fixture_path.name),
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "roles": results,
        "violations": violations,
    }


def _now() -> Any:
    from datetime import datetime, timezone

    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "### Model selection (offline, real selector)",
        "",
        f"Fixture `{report['fixture']}` (sha256 `{report['fixture_sha256'][:12]}`). "
        "Runs the production `EligibilityLadder` on a checked-in inventory: no network, "
        "no live prices, no savings claim.",
        "",
        "| Role | Floor | Selected | Tier | Capacity | Dropped (reason: count) |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["roles"]:
        dropped = ", ".join(f"{k}: {v}" for k, v in row["dropped"].items()) or "none"
        lines.append(
            f"| {row['role']} | ≤{row['floor']} | `{row['selected']}` | {row['tier']} "
            f"| {row['capacity']} | {dropped} |"
        )
    lines.append("")
    if report["violations"]:
        lines.append("**Capability-floor violations:**")
        lines += [f"- {v}" for v in report["violations"]]
    else:
        lines.append("Every role got a model at or above its capability floor.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    args = parser.parse_args(argv)
    report = run(args.fixture)
    markdown = render_markdown(report)
    if args.output_json:
        args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.output_md:
        args.output_md.write_text(markdown)
    print(markdown, end="")
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
