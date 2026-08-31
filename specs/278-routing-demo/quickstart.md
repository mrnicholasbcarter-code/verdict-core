# Quickstart: Routing Demo Cost vs Quality

## Prerequisites

- `verdict-core` with uv
- Reachable OpenAI-compatible gateway (default `http://localhost:20128/v1`) serving `/models` and `/api/pricing`
- For live execute metrics: gateway accepting short chat completions (pressure/503s lower success rate but must not invent successes)

## Live run

```bash
uv run python -m verdict.routing_demo
# or: uv run python verdict/routing_demo.py
```

Expect: 100 decisions, baseline vs routed savings, latency/success, wall clock < 60s when catalog is healthy.

If catalog unreachable: **blocked** (non-zero exit) — do not treat as pass.

## Recorded replay

```bash
uv run python -m verdict.routing_demo --recorded docs/benchmarks/routing-demo-capture.json
```

Output must show `mode=recorded` with capture timestamp.

## Tests

```bash
uv run pytest -q tests/test_routing_demo.py
uv run pytest -q tests/test_routing_demo_live.py   # live; blocked ≠ pass
```

## Must fail

- Green “savings” from MOCK_CATALOG-style invented prices as the demo proof
- Paid chosen while cheaper qualified kept remains
- Silent fixture fallback when live mode requested
