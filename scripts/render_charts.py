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

import io
import json
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

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

# Verdict visual palette (24-bit hex). This mirrors the design-system tokens in
# verdict/design.py (charcoal surfaces, bright text, purple/cyan accents). It is
# hard-coded on purpose: this script must never import verdict/.
PALETTE = {
    "background": "#131316",  # deep charcoal page / card
    "surface": "#1c1c21",  # inner panel
    "border": "#34343d",  # clean panel border
    "grid": "#26262c",
    "text": "#f4f4f5",  # bright primary text
    "secondary": "#a1a1aa",
    "muted": "#71717a",
    "purple": "#7f5bd5",  # primary accent (Verdict arm, admitted)
    "cyan": "#0ea5e9",  # secondary accent (stage flow)
    "amber": "#f59e0b",  # cooldown / capacity pressure
    "red": "#ef4444",  # failure
    "success": "#22c55e",  # validated / success
    "neutral": "#52525b",  # baseline / direct arm
}

WIDTH_IN = 10.0  # ~830 px README width at 83 dpi; SVG scales cleanly
PAD = 0.035  # consistent card padding (fraction of figure width)

plt.rcParams.update(
    {
        "svg.hashsalt": "verdict",  # deterministic ids
        "svg.fonttype": "path",  # text as paths: renders the same without local fonts
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "text.color": PALETTE["text"],
        "axes.labelcolor": PALETTE["secondary"],
        "xtick.color": PALETTE["secondary"],
        "ytick.color": PALETTE["text"],
    }
)


def _card(height_in: float) -> plt.Figure:
    """Charcoal figure with a rounded, bordered panel (contrast in light and dark themes)."""
    fig = plt.figure(figsize=(WIDTH_IN, height_in), facecolor=PALETTE["background"])
    ratio = WIDTH_IN / height_in
    fig.patches.append(
        FancyBboxPatch(
            (0.012, 0.012 * ratio),
            0.976,
            1 - 0.024 * ratio,
            boxstyle=f"round,pad=0,rounding_size={0.018}",
            transform=fig.transFigure,
            facecolor=PALETTE["surface"],
            edgecolor=PALETTE["border"],
            linewidth=1.2,
            zorder=-10,
        )
    )
    return fig


def _header(fig: plt.Figure, height_in: float, eyebrow: str, headline: str, sub: str) -> None:
    top = 1 - 0.34 / height_in
    fig.text(PAD, top, "VERDICT", fontsize=8.5, color=PALETTE["purple"], weight="bold")
    fig.text(PAD + 0.075, top, eyebrow, fontsize=8.5, color=PALETTE["muted"])
    fig.text(PAD, top - 0.36 / height_in, headline, fontsize=17, weight="bold")
    fig.text(PAD, top - 0.62 / height_in, sub, fontsize=9.5, color=PALETTE["secondary"])


def _footer(fig: plt.Figure, height_in: float, source: str, note: str = "") -> None:
    y = 0.22 / height_in
    fig.text(PAD, y, f"Source: {source}", fontsize=8, color=PALETTE["muted"])
    if note:
        fig.text(1 - PAD, y, note, fontsize=8, color=PALETTE["amber"], ha="right")


def _style_axes(ax: plt.Axes) -> None:
    ax.set_facecolor(PALETTE["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(PALETTE["border"])
    ax.tick_params(length=0)
    ax.grid(axis="x", color=PALETTE["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def _legend(ax: plt.Axes) -> None:
    leg = ax.legend(loc="lower right", frameon=False, fontsize=8.5)
    for text in leg.get_texts():
        text.set_color(PALETTE["secondary"])


def _save(fig: plt.Figure, name: str, title: str, desc: str) -> None:
    """Write the SVG and add <title>/<desc> so the text survives as paths."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    fig.savefig(buf, format="svg", facecolor=PALETTE["background"], metadata={"Date": None})
    plt.close(fig)
    svg = buf.getvalue()
    root_end = svg.index(">", svg.index("<svg")) + 1
    meta = f"\n <title>{escape(title)}</title>\n <desc>{escape(desc)}</desc>"
    (ASSETS / name).write_text(svg[:root_end] + meta + svg[root_end:], encoding="utf-8")


def admission_funnel() -> None:
    source = "docs/proof/demo-run/admission.json"
    data = json.loads((ROOT / source).read_text(encoding="utf-8"))
    candidates = data["candidates"]
    remaining = len(candidates)
    labels, counts, drops = ["inventory"], [remaining], [[]]
    for stage in STAGES:
        dropped = [c for c in candidates if c["first_failed_stage"] == stage]
        remaining -= len(dropped)
        labels.append(stage)
        counts.append(remaining)
        drops.append(dropped)
    labels.append("admitted")
    counts.append(len(data["admitted"]))
    drops.append([])

    title = "Canonical admission of the demo inventory (fixture data)"
    headline = f"{len(candidates)} candidates, {len(data['admitted'])} admitted"
    h = 4.6
    fig = _card(h)
    _header(fig, h, "ADMISSION  ·  fixture data", headline, title)
    ax = fig.add_axes((0.2, 0.14, 0.33, 0.62))
    _style_axes(ax)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    y = list(range(len(labels)))[::-1]
    for yi, label, count in zip(y, labels, counts, strict=True):
        final = label == "admitted"
        ax.barh(yi, len(candidates), color=PALETTE["grid"], height=0.62)
        ax.barh(yi, count, color=PALETTE["purple" if final else "cyan"], height=0.62)
        ax.text(
            -0.4,
            yi,
            str(count),
            ha="right",
            va="center",
            fontsize=15,
            weight="bold",
            color=PALETTE["success" if final else "text"],
        )
    ax.set_yticks(y, [""] * len(y))
    ax.set_xticks([])
    ax.set_xlim(0, len(candidates))
    ax.set_ylim(-0.6, len(labels) - 0.4)
    # stage names in their own left column, rejection reasons in one aligned right column
    row_at = ax.get_yaxis_transform()  # x in axes fraction, y in data rows

    def ax_x(fig_x: float) -> float:
        return (fig_x - 0.2) / 0.33

    for yi, label in zip(y, labels, strict=True):
        ax.text(
            ax_x(PAD),
            yi,
            label,
            va="center",
            fontsize=9.5,
            color=PALETTE["secondary"],
            transform=row_at,
        )
    col = 0.57
    ax.text(
        ax_x(col),
        len(labels) - 0.45,
        "REJECTED AT STAGE",
        fontsize=8,
        color=PALETTE["muted"],
        transform=row_at,
    )
    for yi, dropped in zip(y, drops, strict=True):
        if not dropped:
            continue
        reason = ", ".join(sorted({c["reason"] for c in dropped}))
        routes = ", ".join(c["route_id"] for c in dropped)
        ax.text(
            ax_x(col),
            yi,
            f"-{len(dropped)}",
            va="center",
            fontsize=11,
            weight="bold",
            color=PALETTE["red"],
            transform=row_at,
        )
        ax.text(
            ax_x(col + 0.04),
            yi,
            reason,
            va="center",
            fontsize=9.5,
            color=PALETTE["text"],
            transform=row_at,
        )
        ax.text(
            ax_x(col + 0.23),
            yi,
            routes,
            va="center",
            fontsize=8.5,
            color=PALETTE["muted"],
            transform=row_at,
        )
    _footer(fig, h, source, "fixture data")
    desc = "; ".join(
        f"{lab} {cnt}" + (f" (-{c['route_id']} ({c['reason']}))" if (c := (d or [None])[0]) else "")
        for lab, cnt, d in zip(labels, counts, drops, strict=True)
    )
    _save(fig, "chart-admission-funnel.svg", f"{headline}. {title}", f"{desc}. Source: {source}")


def recovery() -> None:
    source = "docs/proof/demo-run/receipt.json"
    receipt = json.loads((ROOT / source).read_text(encoding="utf-8"))
    nodes = [n for n in receipt["nodes"] if n["kind"] == "implement"]
    title = "Same-node reassignment in the demo run (fixture data, injected faults)"
    attempts = sum(len(n["attempts"]) for n in nodes)
    headline = f"{len(nodes)} nodes, {attempts} attempts, all validated"
    if not all(n["attempts"][-1]["outcome"] == "success" for n in nodes):
        headline = f"{len(nodes)} nodes, {attempts} attempts"
    h = 2.3 + 1.0 * len(nodes)
    fig = _card(h)
    _header(fig, h, "RECOVERY  ·  fixture data, injected faults", headline, title)
    ax = fig.add_axes((0.14, 0.55 / h, 0.83, 1 - 1.95 / h))
    _style_axes(ax)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    desc_rows = []
    for row, node in enumerate(reversed(nodes)):
        parts = []
        for attempt in node["attempts"]:
            x = attempt["attempt"] - 1
            ok = attempt["outcome"] == "success"
            label = "validated" if ok else attempt.get("failure_category", "failure")
            if attempt.get("fault_injected"):
                label += " (injected)"
            edge = PALETTE["success" if ok else "red"]
            ax.add_patch(
                FancyBboxPatch(
                    (x + 0.03, row - 0.36),
                    0.88,
                    0.72,
                    boxstyle="round,pad=0,rounding_size=0.06",
                    facecolor=PALETTE["background"],
                    edgecolor=edge,
                    linewidth=1.6,
                )
            )
            ax.add_patch(plt.Rectangle((x + 0.03, row - 0.36), 0.025, 0.72, color=edge))
            ax.text(
                x + 0.09,
                row + 0.17,
                attempt["route_id"],
                va="center",
                fontsize=9.5,
                weight="bold",
                color=PALETTE["text"],
            )
            ax.text(
                x + 0.09, row - 0.03, label, va="center", fontsize=8.5, color=edge, weight="bold"
            )
            cap = PALETTE["amber"] if attempt["capacity_class"] != "free" else PALETTE["secondary"]
            ax.text(
                x + 0.09, row - 0.22, attempt["capacity_class"], va="center", fontsize=8, color=cap
            )
            if attempt is not node["attempts"][-1]:
                ax.annotate(
                    "",
                    (x + 1.03, row),
                    (x + 0.91, row),
                    arrowprops={"arrowstyle": "->", "color": PALETTE["muted"], "lw": 1.2},
                )
            parts.append(f"{attempt['route_id']} {attempt['capacity_class']} {label}")
        desc_rows.append(f"{node['node_id']}: " + " -> ".join(parts))
    width = max(len(n["attempts"]) for n in nodes)
    ax.set_yticks(range(len(nodes)), [n["node_id"] for n in reversed(nodes)], fontsize=10.5)
    ax.set_xticks([i + 0.47 for i in range(width)], [f"attempt {i + 1}" for i in range(width)])
    ax.xaxis.tick_top()
    ax.set_xlim(0, width)
    ax.set_ylim(-0.5, len(nodes) - 0.5)
    _footer(fig, h, source, "fixture data, injected faults")
    _save(
        fig,
        "chart-recovery.svg",
        f"{headline}. {title}",
        "; ".join(reversed(desc_rows)) + f". Source: {source}",
    )


def _paired_bars(
    ax: plt.Axes, names: list[str], a: list[float], b: list[float], a_label: str, b_label: str
) -> list[int]:
    y = list(range(len(names)))[::-1]
    ax.barh([i + 0.18 for i in y], a, height=0.34, color=PALETTE["neutral"], label=a_label)
    ax.barh([i - 0.18 for i in y], b, height=0.34, color=PALETTE["purple"], label=b_label)
    ax.set_yticks(y, names, fontsize=9.5)
    ax.xaxis.label.set_size(8.5)
    return y


def paired_fixture() -> None:
    source = "benchmarks/fixtures/legit_paired_savings.json"
    tasks = json.loads((ROOT / source).read_text(encoding="utf-8"))["tasks"]
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
    title = "Paired-savings fixture: stated costs, simulation only, claims_allowed=false"
    h = 4.4
    fig = _card(h)
    _header(fig, h, "COST  ·  fixture", "Stated costs only, no savings claimed", title)
    ax = fig.add_axes((0.27, 0.2, 0.69, 0.53))
    _style_axes(ax)
    y = _paired_bars(ax, names, direct, routed, "direct arm", "Verdict arm")
    for yi, d, note in zip(y, direct, notes, strict=True):
        if note:
            ax.text(d + 0.0006, yi - 0.18, note, va="center", fontsize=8.5, color=PALETTE["amber"])
    ax.set_xlabel("stated cost per task, USD (fixture values, not observed)")
    ax.set_xlim(0, max(direct) * 1.6)
    _legend(ax)
    _footer(fig, h, source, "claims_allowed=false")
    desc = "; ".join(
        f"{n}: direct {d}, Verdict {r}" + (f" ({note})" if note else "")
        for n, d, r, note in zip(names, direct, routed, notes, strict=True)
    )
    _save(fig, "chart-paired-fixture.svg", title, f"{desc}. Source: {source}")


def live_savings() -> None:
    source = "docs/proof/live-savings-2026-09-28/report.json"
    report = json.loads((ROOT / source).read_text(encoding="utf-8"))
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
    title = f"Live cost check: {headline}, n={len(names)} tasks"
    h = 1.9 + 0.36 * len(names)
    fig = _card(h)
    big = (
        "No routing saving measured" if same else f"{summary['savings_pct']}% list-price difference"
    )
    _header(fig, h, "COST  ·  live, list price", big, title)
    ax = fig.add_axes((0.2, 0.75 / h, 0.76, 1 - 1.75 / h))
    _style_axes(ax)
    y = _paired_bars(ax, names, baseline_costs, verdict_costs, "baseline (opus-5)", "Verdict arm")
    for yi, vc, vm in zip(y, verdict_costs, verdict_models, strict=True):
        ax.text(vc + 0.001, yi - 0.18, vm, va="center", fontsize=7.5, color=PALETTE["secondary"])
    ax.set_xlabel("list-price cost per task, USD (observed tokens x published list price)")
    ax.set_xlim(0, max(baseline_costs) * 1.5)
    _legend(ax)
    _footer(fig, h, source, "not billed amounts")
    desc = "; ".join(
        f"{n}: baseline {b}, Verdict {v} ({m})"
        for n, b, v, m in zip(names, baseline_costs, verdict_costs, verdict_models, strict=True)
    )
    _save(fig, "chart-live-savings.svg", title, f"{desc}. Source: {source}")


def main() -> None:
    admission_funnel()
    recovery()
    paired_fixture()
    live_savings()
    for name in CHART_SOURCES:
        print(f"wrote docs/assets/{name} ({(ASSETS / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
