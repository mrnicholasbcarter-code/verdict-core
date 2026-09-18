"""Streamlit analytics dashboard for the Verdict decision log.

Spend truthfulness (BOD-113): "actual" spend is shown only from observed
cost/token receipts recorded on decisions. Any per-model price assumption is
labelled synthetic, is opt-in, and is never presented as measured cost.
"""

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

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


# Observed receipts: the only inputs allowed to back "actual spend".
OBSERVED_COST_FIELDS = ("observed_cost_usd", "cost_usd")
OBSERVED_TOKEN_FIELDS = ("observed_tokens_total", "tokens_total")

# Synthetic assumptions: opt-in illustration only, never labelled actual.
SYNTHETIC_PRICES_USD_PER_REQUEST = {
    "opus": 0.015,
    "sonnet": 0.003,
    "flash": 0.00035,
    "8b": 0.00035,
    "default": 0.001,
}
SYNTHETIC_FRONTIER_BASELINE_USD = 0.015


def _first_number(record: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Return the first numeric value among ``keys`` at the top level or in the receipt."""
    receipt = record.get("admit_receipt")
    scopes: list[dict[str, Any]] = [record]
    if isinstance(receipt, dict):
        scopes.append(receipt)
    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return float(value)
    return None


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
    df["observed_cost_usd"] = [_first_number(row, OBSERVED_COST_FIELDS) for row in records]
    df["observed_tokens_total"] = [_first_number(row, OBSERVED_TOKEN_FIELDS) for row in records]
    df["cost_source"] = [
        "observed receipt" if cost is not None else "not measured"
        for cost in df["observed_cost_usd"]
    ]
    return df


df = load_data(log_path)

if df.empty:
    st.info("Log is empty.")
    st.stop()

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
        "Measured Spend (observed receipts)",
        f"${measured_spend:,.4f}",
        help=f"Sum of observed cost receipts on {measured_count} of {total_requests} decisions.",
    )
else:
    col2.metric(
        "Measured Spend (observed receipts)",
        "not measured",
        help=(
            "No decision in this log carries an observed cost receipt "
            "(observed_cost_usd). Verdict does not estimate spend from model names."
        ),
    )
col3.metric("P99 Routing Latency", f"{p99_latency:.2f}ms")

if measured_count < total_requests:
    st.caption(
        f"{total_requests - measured_count} of {total_requests} decisions have no observed "
        "cost receipt and are excluded from measured spend. Savings are never inferred "
        "from model-name price tables."
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
        df_time = measured.set_index("ts").resample("1H")[["observed_cost_usd"]].sum().reset_index()
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
