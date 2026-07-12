import json
import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="llm-gate Analytics", page_icon="⚙️", layout="wide")

st.title("⚙️ llm-gate Analytics & Savings")
st.markdown("Live routing dashboard for evaluating heuristic fallbacks and quota headroom.")

log_path = st.text_input("Path to JSONL Log:", value="llm-gate-decisions.jsonl")

if not os.path.exists(log_path):
    st.warning(f"Log file not found at {log_path}. Run some tasks first!")
    st.stop()


# Parse Data
@st.cache_data(ttl=5)  # type: ignore[untyped-decorator]
def load_data(path: str) -> pd.DataFrame:
    records = []
    import os

    if not os.path.exists(path):
        return pd.DataFrame()
    with open(path) as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["ts"] = pd.to_datetime(df["ts"])

    # Mock Costs (Opus: $15/M, Sonnet: $3/M, Flash: $0.35/M)
    def calc_cost(model: str) -> float:
        if "opus" in model:
            return 0.015
        if "sonnet" in model:
            return 0.003
        if "flash" in model or "8b" in model:
            return 0.00035
        return 0.001

    df["estimated_cost"] = df["model_chosen"].apply(calc_cost)
    # Compare against routing everything to Opus
    df["primary_cost_baseline"] = 0.015
    df["savings"] = df["primary_cost_baseline"] - df["estimated_cost"]

    return df


df = load_data(log_path)

if df.empty:
    st.info("Log is empty.")
    st.stop()

# High level KPIs
total_requests = len(df)
total_savings = df["savings"].sum()
p99_latency = df["latency_ms"].quantile(0.99)

col1, col2, col3 = st.columns(3)
col1.metric("Total Routed Prompts", f"{total_requests:,}")
col2.metric("Estimated Savings vs Core Model", f"${total_savings:,.2f}")
col3.metric("P99 Routing Latency", f"{p99_latency:.2f}ms")

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
    st.subheader("Cost Savings Over Time")
    df_time = (
        df.set_index("ts")
        .resample("1H")[["estimated_cost", "primary_cost_baseline"]]
        .sum()
        .reset_index()
    )
    fig2 = go.Figure()
    fig2.add_trace(
        go.Scatter(
            x=df_time["ts"],
            y=df_time["primary_cost_baseline"],
            fill="tozeroy",
            name="Cost without llm-gate (Opus)",
            fillcolor="rgba(255,0,0,0.1)",
            line=dict(color="red", dash="dot"),
        )
    )
    fig2.add_trace(
        go.Scatter(
            x=df_time["ts"],
            y=df_time["estimated_cost"],
            fill="tozeroy",
            name="Actual Cost with llm-gate",
            fillcolor="rgba(0,255,0,0.2)",
            line=dict(color="green"),
        )
    )
    fig2.update_layout(title="Cumulative Spend Drift", hovermode="x unified")
    st.plotly_chart(fig2, use_container_width=True)

st.divider()
st.subheader("Raw Decision Log")
st.dataframe(
    df[["ts", "task_preview", "input_tier", "model_chosen", "reason", "savings"]].sort_values(
        "ts", ascending=False
    ),
    use_container_width=True,
)
