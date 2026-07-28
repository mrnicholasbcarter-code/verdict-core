# Kalshi Automated Trading Systems — Technical Case Study

## Executive Summary

This case study documents the design, architecture, execution pipeline, and risk bounds of automated trading bots developed for **Kalshi prediction markets** (`prediction-market-sdk`). The system executes probability arbitrage and event-driven market making on binary contract markets using an ultra-low-latency Python async stack.

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ KALSHI WEBSOCKET L2 FEEDS (Orderbook Deltas & Ticker Updates)│
└─────────────────────────────────────────────────────────────┘
                              │ Async WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ ZERO-ALLOCATION PARSER (msgspec + Python AsyncIO)          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ IN-MEMORY ORDERBOOK & PROBABILITY PRICING ENGINE            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ REAL-TIME RISK GATE (Position Caps, Slippage, Drawdown)     │
└─────────────────────────────────────────────────────────────┘
                              │ REST Order Execution
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ KALSHI REST API (Limit Orders, Cancellations, Fills Audit)  │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Technical Stack & Implementation Details

- **Language & Runtime**: Python 3.12 / CPython with `asyncio` event loop.
- **Serialization**: `msgspec` for struct decoding without Python dict instantiation overhead, processing >100,000 WebSocket messages/sec.
- **Feed Handler**: Dedicated async task managing persistent WebSocket connection with exponential backoff reconnect and orderbook snapshot resynchronization.
- **Orderbook Engine**: In-memory sparse price-level map tracking bid/ask depth, spread dynamics, and implied contract probability.

---

## 3. Trading Strategies & Signal Generation

### A. Implied Probability Arbitrage
Calculates contract pricing dislocations between related prediction market outcomes (e.g., economic data releases, political events, Fed interest rate decisions) and places limit orders when pricing strays beyond probability boundaries.

### B. Event-Driven Market Making
Provides liquidity on binary contract orderbooks by placing symmetric bid/ask limit orders around fair probability, capturing spread while dynamically skewing quotes based on net inventory.

---

## 4. Risk Engineering & Guardrails

- **Hard Position Caps**: Enforces strict maximum exposure limits per market contract and aggregated portfolio.
- **Slippage Bounds**: Rejects limit orders if top-of-book depth shifts beyond configured tolerance during order generation.
- **Automated Circuit Breaker**: Instantly halts strategy execution and cancels open orders if cumulative session loss exceeds configured drawdown threshold.
- **Disconnection Handler**: Automatically cancels open resting orders if WebSocket heartbeat fails for >3 seconds.

---

## 5. Operational Lessons & Engineering Insights

1. **Python Performance**: Using `msgspec` structures instead of standard `json.loads` reduced message parsing latency by 82%.
2. **Order Rejection Resiliency**: Implemented nonce tracking and idempotent order placement to recover cleanly from transient API rate-limit errors (HTTP 429).
3. **Event Volatility**: High-volatility news events require asymmetric spread widening rather than aggressive quoting to avoid adverse selection.
