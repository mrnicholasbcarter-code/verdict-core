# Routing demo: cost vs quality (#278)

Runnable portfolio demo: exactly **100** heterogeneous requests, cheaper-first routing reused from Feature 276 live-routing, aggregate savings versus the costliest qualified baseline, plus latency/success from bounded live executes.

## Run (no-spend mock)

```bash
uv run python -m verdict.routing_demo --mock
uv run python -m verdict.routing_demo --mock --json
```

The mock mode satisfies the absorbed #282 comparison: exactly 100 deterministic requests, fixed Opus/Sonnet/Haiku costs versus a class-aware auto route, terminal and JSON output, eligibility-gated adaptive-ranker evidence, and no provider calls. Prices are labeled estimates from Anthropic's pricing page, observed 2026-08-31.

## Run (live)

```bash
uv run python -m verdict.routing_demo
uv run python -m verdict.routing_demo --json
uv run python -m verdict.routing_demo --save-capture docs/benchmarks/routing-demo-capture.json
```

Requires OmniRoute-compatible gateway at `http://localhost:20128/v1` (`/models` + `/api/pricing`). Only text/chat identities qualify; tool-required requests require an observed tools capability. Selected identities must pass an exact named JSON check or the demo exits **blocked** without claiming savings.

## Recorded replay

```bash
uv run python -m verdict.routing_demo --recorded docs/benchmarks/routing-demo-capture.json --no-execute
```

Output is labeled `mode=recorded` with `catalog_captured_at`.

## Interpret

- **routed**: sum of estimated USD using live/recorded published prices on cheaper-first choices
- **baseline**: same estimator on the costliest still-qualified identity per request
- **savings**: baseline − routed
- **success_rate / avg_latency_ms**: live mode requires the named chat check to pass for every selected identity; mock mode reports deterministic adapter-contract quality and is labeled `mode=mock`
- **adaptive_ranker**: observe-only shadow evidence over the eligibility-gated mock candidates; it does not override the class-aware demo route

The historical `scripts/demo-routing.py` is not used; the supported mock and live modes share this module and output contract.
