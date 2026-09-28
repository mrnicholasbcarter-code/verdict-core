"""Render README charts from committed data only.

Inputs (all committed, all fixture data, no live observations):

* docs/proof/demo-run/admission.json: canonical admission receipt of the demo
  fixture inventory (admission funnel chart).
* docs/proof/demo-run/receipt.json: run receipt of the demo run (recovery
  chart: every attempt per node, its route, capacity class and outcome).
* benchmarks/fixtures/legit_paired_savings.json: STATED per-task costs of the
  offline paired-savings simulation. They are not observed and the bench
  itself refuses to claim savings from them (claims_allowed=false).

Run from a throwaway environment so matplotlib never enters the project:

    uv run --with matplotlib==3.10.* --no-project python scripts/render_charts.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "docs" / "proof" / "demo-run"
ASSETS = ROOT / "docs" / "assets"

# Source file for each chart. tests/test_readme_assets.py checks these exist.
CHART_SOURCES = {
    "chart-admission-funnel.svg": ("docs/proof/demo-run/admission.json",),
    "chart-recovery.svg": ("docs/proof/demo-run/receipt.json",),
    "chart-paired-fixture.svg": ("benchmarks/fixtures/legit_paired_savings.json",),
    "chart-live-savings.svg": ("docs/proof/live-savings-2026-09-28/report.json",),
}

STAGES = ("DISCOVERED", "ENTITLED", "HEALTHY", "AVAILABLE")
COLORS = {"success": "#2e7d32", "failure": "#c62828", "bar": "#1565c0", "drop": "#9e9e9e"}

plt.rcParams.update(
    {
        "svg.hashsalt": "verdict",  # deterministic ids
        "svg.fonttype": "none",
        "font.family": "DejaVu Sans",
        "font.size": 10,
    }
)


def _save(fig: plt.Figure, name: str) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    fig.savefig(ASSETS / name, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def admission_funnel() -> None:
    data = json.loads((RUN / "admission.json").read_text(encoding="utf-8"))
    candidates = data["candidates"]
    remaining = len(candidates)
    labels, counts, notes = ["inventory"], [remaining], [""]
    for stage in STAGES:
        dropped = [c for c in candidates if c["first_failed_stage"] == stage]
        remaining -= len(dropped)
        labels.append(stage)
        counts.append(remaining)
        notes.append(", ".join(f"-{c['route_id']} ({c['reason']})" for c in dropped))
    labels.append("admitted")
    counts.append(len(data["admitted"]))
    notes.append("")

    fig, ax = plt.subplots(figsize=(9, 3.6))
    y = list(range(len(labels)))[::-1]
    ax.barh(y, counts, color=COLORS["bar"], height=0.6)
    for yi, count, note in zip(y, counts, notes, strict=True):
        ax.text(count + 0.15, yi, f"{count}  {note}", va="center", fontsize=8.5)
    ax.set_yticks(y, labels)
    ax.set_xlim(0, max(counts) + 9)
    ax.set_xticks(range(0, max(counts) + 1, 2))
    ax.set_xlabel("routes remaining")
    ax.set_title(
        "Canonical admission of the demo inventory (fixture data)", loc="left", fontsize=11
    )
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    _save(fig, "chart-admission-funnel.svg")


def recovery() -> None:
    receipt = json.loads((RUN / "receipt.json").read_text(encoding="utf-8"))
    nodes = [n for n in receipt["nodes"] if n["kind"] == "implement"]
    fig, ax = plt.subplots(figsize=(9, 1.2 + 0.9 * len(nodes)))
    for row, node in enumerate(reversed(nodes)):
        for attempt in node["attempts"]:
            x = attempt["attempt"] - 1
            ok = attempt["outcome"] == "success"
            ax.barh(row, 0.94, left=x, color=COLORS["success" if ok else "failure"], height=0.6)
            label = "validated" if ok else attempt.get("failure_category", "failure")
            if attempt.get("fault_injected"):
                label += " (injected)"
            ax.text(
                x + 0.47,
                row + 0.08,
                attempt["route_id"],
                ha="center",
                color="white",
                fontsize=8.5,
                fontweight="bold",
            )
            ax.text(
                x + 0.47,
                row - 0.18,
                f"{attempt['capacity_class']} | {label}",
                ha="center",
                color="white",
                fontsize=7.5,
            )
    ax.set_yticks(range(len(nodes)), [n["node_id"] for n in reversed(nodes)])
    width = max(len(n["attempts"]) for n in nodes)
    ax.set_xticks([i + 0.47 for i in range(width)], [f"attempt {i + 1}" for i in range(width)])
    ax.set_xlim(0, width)
    ax.set_title(
        "Same-node reassignment in the demo run (fixture data, injected faults)",
        loc="left",
        fontsize=11,
    )
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(left=False)
    _save(fig, "chart-recovery.svg")


def paired_fixture() -> None:
    path = ROOT / "benchmarks" / "fixtures" / "legit_paired_savings.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))["tasks"]
    names = [t["id"] for t in tasks]
    direct = [float(t["direct"]["headers"]["X-OmniRoute-Response-Cost"]) for t in tasks]
    routed = [float(t["verdict"]["headers"]["X-OmniRoute-Response-Cost"]) for t in tasks]
    notes = []
    for t in tasks:
        v = t["verdict"]
        if v["headers"].get("X-OmniRoute-Cache-Hit") == "true":
            notes.append("cache hit: not model savings")
        elif not v["quality"]["passed"]:
            notes.append("quality miss: no claim")
        else:
            notes.append("")
    fig, ax = plt.subplots(figsize=(9, 3.4))
    y = list(range(len(tasks)))[::-1]
    ax.barh([i + 0.18 for i in y], direct, height=0.34, color=COLORS["drop"], label="direct arm")
    ax.barh([i - 0.18 for i in y], routed, height=0.34, color=COLORS["bar"], label="Verdict arm")
    for yi, d, note in zip(y, direct, notes, strict=True):
        if note:
            ax.text(d + 0.0006, yi - 0.18, note, va="center", fontsize=8, color=COLORS["failure"])
    ax.set_yticks(y, names)
    ax.set_xlabel("stated cost per task, USD (fixture values, not observed)")
    ax.set_xlim(0, max(direct) * 1.6)
    ax.legend(loc="lower right", frameon=False, fontsize=8.5)
    ax.set_title(
        "Paired-savings fixture: stated costs, simulation only, claims_allowed=false",
        loc="left",
        fontsize=11,
    )
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    _save(fig, "chart-paired-fixture.svg")


def live_savings() -> None:
    path = ROOT / "docs" / "proof" / "live-savings-2026-09-28" / "report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    per_task = report["per_task"]
    summary = report["summary"]
    # Schema v2 uses cost_eligible_pairs (list of "task/rN" strings)
    eligible_pairs = set(summary.get("cost_eligible_pairs", []))
    # Backward compat: v1 used cost_eligible_tasks (list of task ids)
    eligible_tasks = set(summary.get("cost_eligible_tasks", []))

    names: list[str] = []
    baseline_costs: list[float] = []
    verdict_costs: list[float] = []
    verdict_models: list[str] = []

    for entry in per_task:
        task_id = entry["task_id"]
        # Check if any pair for this task is eligible
        task_eligible = (
            any(p.startswith(f"{task_id}/") for p in eligible_pairs) or task_id in eligible_tasks
        )
        if not task_eligible:
            continue
        b_runs = [r for r in entry["runs"] if r["arm"] == "baseline" and r.get("cost_usd")]
        v_runs = [r for r in entry["runs"] if r["arm"] == "verdict" and r.get("cost_usd")]
        if not b_runs or not v_runs:
            continue
        names.append(entry["task_id"])
        baseline_costs.append(b_runs[0]["cost_usd"])
        verdict_costs.append(v_runs[0]["cost_usd"])
        verdict_models.append(v_runs[0]["model"].split("/")[-1])

    fig, ax = plt.subplots(figsize=(10, 4.0))
    y = list(range(len(names)))[::-1]
    ax.barh(
        [i + 0.18 for i in y],
        baseline_costs,
        height=0.34,
        color=COLORS["drop"],
        label="baseline (opus-5)",
    )
    ax.barh(
        [i - 0.18 for i in y], verdict_costs, height=0.34, color=COLORS["bar"], label="Verdict arm"
    )
    for yi, vc, vm in zip(y, verdict_costs, verdict_models, strict=True):
        ax.text(vc + 0.001, yi - 0.18, vm, va="center", fontsize=7, color="#555")
    ax.set_yticks(y, names)
    ax.set_xlabel("list-price cost per task, USD (observed tokens x published list price)")
    ax.set_xlim(0, max(baseline_costs) * 1.5)
    ax.legend(loc="lower right", frameon=False, fontsize=8.5)
    # Both arms can run the same model on the same prompt; then no routing saving
    # exists to show, so the title says so instead of printing a percentage.
    arm_models = {
        arm: {str(r.get("model")) for e in per_task for r in e["runs"] if r["arm"] == arm}
        for arm in ("baseline", "verdict")
    }
    same = arm_models["baseline"] == arm_models["verdict"]
    headline = (
        f"no routing saving measured (both arms on {', '.join(sorted(arm_models['verdict']))})"
        if same
        else f"{summary['savings_pct']}% list-price difference over pairs where both arms passed"
    )
    ax.set_title(f"Live cost check: {headline}, n={len(names)} tasks", loc="left", fontsize=11)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    _save(fig, "chart-live-savings.svg")


def main() -> None:
    admission_funnel()
    recovery()
    paired_fixture()
    live_savings()
    for name in CHART_SOURCES:
        print(f"wrote docs/assets/{name} ({(ASSETS / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
