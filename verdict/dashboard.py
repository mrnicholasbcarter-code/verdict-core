"""Streamlit analytics dashboard for the Verdict decision log.

Spend truthfulness (BOD-113 / BOD-117): "measured" spend is shown only from
post-execution outcome receipts (``verdict-outcomes.jsonl``, written by the
serve path after the gateway answers and joined to decisions by
``request_id``). The pre-execution decision row cannot know what an execution
cost, so it is never a spend source. Any per-model price assumption is
labelled synthetic, is opt-in, and is never presented as measured cost.
"""

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from verdict.outcome_log import load_outcomes, measured_spend_for, outcome_log_path

st.set_page_config(page_title="verdict Analytics", page_icon="⚙️", layout="wide")

st.title("⚙️ verdict Analytics")
st.markdown("Live routing dashboard for evaluating heuristic fallbacks and quota headroom.")

available_logs = sorted(Path.cwd().glob("*.jsonl"))
default_log = Path.cwd() / "verdict-decisions.jsonl"
if default_log not in available_logs:
    available_logs.insert(0, default_log)
log_path = st.selectbox("Decision log", available_logs, format_func=str)

if not log_path.is_file():
    st.warning(f"Log file not found at {log_path}. Run some tasks first!")
    st.stop()


# Synthetic assumptions: opt-in illustration only, never labelled actual.
SYNTHETIC_PRICES_USD_PER_REQUEST = {
    "opus": 0.015,
    "sonnet": 0.003,
    "flash": 0.00035,
    "8b": 0.00035,
    "default": 0.001,
}
SYNTHETIC_FRONTIER_BASELINE_USD = 0.015


def synthetic_price(model: str) -> float:
    """Assumed per-request price by model-name pattern. Synthetic, not measured."""
    lowered = model.lower()
    for token, price in SYNTHETIC_PRICES_USD_PER_REQUEST.items():
        if token != "default" and token in lowered:
            return price
    return SYNTHETIC_PRICES_USD_PER_REQUEST["default"]


# Parse Data
@st.cache_data(ttl=5)  # type: ignore[untyped-decorator]
def load_data(path: Path) -> pd.DataFrame:
    records = []

    if not path.is_file():
        return pd.DataFrame()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["ts"] = pd.to_datetime(df["ts"])
    # Join each pre-execution decision to its post-execution outcome receipt.
    # Only that receipt may back "measured": it is the one written after the
    # gateway reported what the execution actually cost.
    outcomes = load_outcomes(path)
    joined = [measured_spend_for(row, outcomes) for row in records]
    df["observed_cost_usd"] = [item["observed_cost_usd"] if item else None for item in joined]
    df["observed_tokens_total"] = [
        item["observed_tokens_total"] if item else None for item in joined
    ]
    df["cost_source"] = [item["cost_source"] if item else "not measured" for item in joined]
    df["has_outcome_receipt"] = [
        isinstance(row.get("request_id"), str) and row["request_id"] in outcomes for row in records
    ]
    return df


df = load_data(log_path)

if df.empty:
    st.info("Log is empty.")
    st.stop()

outcomes_path = outcome_log_path(log_path)
receipt_count = int(df["has_outcome_receipt"].sum())

# High level KPIs (measured only)
total_requests = len(df)
measured = df[df["observed_cost_usd"].notna()]
measured_count = len(measured)
measured_spend = float(measured["observed_cost_usd"].sum()) if measured_count else 0.0
p99_latency = df["latency_ms"].quantile(0.99)

col1, col2, col3 = st.columns(3)
col1.metric("Total Routed Prompts", f"{total_requests:,}")
if measured_count:
    col2.metric(
        f"Measured Spend ({measured_count} receipts)",
        f"${measured_spend:,.4f}",
        help=(
            f"Sum of observed cost on {measured_count} of {total_requests} decisions, "
            f"from post-execution outcome receipts in {outcomes_path.name} "
            "(X-OmniRoute-Response-Cost). Never estimated from model names."
        ),
    )
elif not outcomes_path.is_file():
    col2.metric(
        "Measured Spend",
        "no execution receipts yet",
        help=(
            f"{outcomes_path.name} does not exist beside this decision log. The serve "
            "path writes one outcome receipt per upstream attempt; run traffic through "
            "`verdict serve` to produce them. Verdict does not estimate spend from model names."
        ),
    )
else:
    col2.metric(
        "Measured Spend",
        "not measured",
        help=(
            f"{receipt_count} of {total_requests} decisions have an outcome receipt, but none "
            "carried X-OmniRoute-Response-Cost. The gateway did not report cost; Verdict "
            "does not estimate it from model names."
        ),
    )
col3.metric("P99 Routing Latency", f"{p99_latency:.2f}ms")

if measured_count < total_requests:
    unmeasured = total_requests - measured_count
    without_receipt = total_requests - receipt_count
    st.caption(
        f"{unmeasured} of {total_requests} decisions are excluded from measured spend: "
        f"{without_receipt} have no execution receipt (never executed, streamed before the "
        f"receipt landed, or logged before {outcomes_path.name} existed) and "
        f"{unmeasured - without_receipt} executed without a cost header. Savings are never "
        "inferred from model-name price tables."
    )

st.divider()

# Interactive Charts
c1, c2 = st.columns(2)

with c1:
    st.subheader("Routing Pipeline Flow")
    # Sunburst: Tier -> Provider -> Model
    fig = px.sunburst(
        df,
        path=["effective_tier", "provider", "model_chosen"],
        title="Distribution of LLM Offloading",
        color="effective_tier",
        color_continuous_scale="Blues",
    )
    st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("Measured Spend Over Time")
    if measured_count:
        df_time = measured.set_index("ts").resample("1h")[["observed_cost_usd"]].sum().reset_index()
        fig2 = go.Figure()
        fig2.add_trace(
            go.Scatter(
                x=df_time["ts"],
                y=df_time["observed_cost_usd"],
                fill="tozeroy",
                name="Measured spend (observed receipts)",
                fillcolor="rgba(0,255,0,0.2)",
                line=dict(color="green"),
            )
        )
        fig2.update_layout(title="Observed spend from receipts", hovermode="x unified")
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info(
            "No observed cost receipts in this log. Run `verdict benchmark --savings` with a "
            "live paired executor to collect measured costs; this chart never uses assumed prices."
        )

show_synthetic = st.checkbox(
    "Show SYNTHETIC illustration (assumed per-request prices; NOT measured spend)", value=False
)
if show_synthetic:
    st.warning(
        "Synthetic assumptions: prices below are hardcoded illustrative constants keyed on "
        "model-name patterns and a flat frontier baseline. They are not receipts, not "
        "measurements, and must not be quoted as savings."
    )
    st.json(
        {
            "assumed_price_usd_per_request": SYNTHETIC_PRICES_USD_PER_REQUEST,
            "assumed_frontier_baseline_usd_per_request": SYNTHETIC_FRONTIER_BASELINE_USD,
        }
    )
    synthetic = df[["ts", "model_chosen"]].copy()
    synthetic["synthetic_assumed_cost_usd"] = synthetic["model_chosen"].apply(synthetic_price)
    synthetic["synthetic_frontier_baseline_usd"] = SYNTHETIC_FRONTIER_BASELINE_USD
    st.dataframe(synthetic.sort_values("ts", ascending=False), use_container_width=True)

st.divider()
st.subheader("Raw Decision Log")
columns = [
    "ts",
    "task_preview",
    "input_tier",
    "model_chosen",
    "reason",
    "observed_cost_usd",
    "observed_tokens_total",
    "cost_source",
]
st.dataframe(df[columns].sort_values("ts", ascending=False), use_container_width=True)
