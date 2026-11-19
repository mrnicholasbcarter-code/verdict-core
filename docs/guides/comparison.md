# Verdict vs LiteLLM, OpenRouter, and Portkey

Scan this in one minute. They move traffic. Verdict decides whether a model is allowed to take it.

Compared 2026-09-06 against public product pages and 2026 gateway roundups. Numbers below (model counts, fees, stars) are theirs, not ours, and will drift.

## One line each

| Tool | What it is |
|------|------------|
| **LiteLLM** | Self-hosted OpenAI-compatible proxy. One API in front of many providers. Load-balance, retry, log spend. |
| **OpenRouter** | Hosted catalog and billing. One key, many models, provider fallback. You pay inference plus their credit fee. |
| **Portkey** | Gateway plus observability and guardrails. Cloud or self-host the core. |
| **Verdict** | Fail-closed admission control. Named drop reasons. A receipt. Does not replace a proxy or a catalog. |

LiteLLM/OpenRouter/Portkey **route or catalog**. Verdict **admits or blocks**.

## What each actually decides

| Question | LiteLLM | OpenRouter | Portkey | Verdict |
|----------|---------|------------|---------|---------|
| Can I call many providers with one client? | Yes | Yes | Yes | No — sit it in front of one of them |
| Does unknown health still get a model? | Often yes (fallback/retry) | Often yes (provider routing) | Guardrails optional | **No.** Unknown is not healthy |
| Named reason a candidate was dropped? | Logs, not a gate receipt | Usage logs | Traces / guardrail hits | **Yes, on every drop** |
| Can a score put an excluded model back? | Routing config can | Marketplace can | Policy can | **No** |
| Offline proof with no API key? | No | No | No | `verdict quickstart --dry-run` |

## How they stack

Typical shape:

```text
Claude Code / your app
        ↓
     Verdict          ← admit / block + receipt
        ↓
 LiteLLM or OpenRouter or Portkey
        ↓
   provider APIs
```

Use LiteLLM if you want to **operate** the hop. Use OpenRouter if you want a **hosted catalog**. Use Portkey if you want **observability and guardrails as the product**. Use Verdict if you need a **deterministic no** that a dashboard cannot override.

You can run Verdict with OmniRoute (or another OpenAI-compatible gateway) as the catalog. That is the [golden path](golden-path.md), not a replacement for those three.

## What we do not claim

- We do not claim a percent cost cut. No cost saving is claimed at all: see [Limits](../../README.md#limits).
- We do not claim more models than OpenRouter. Catalog size is theirs. Qualification is ours.
- We do not claim to be a coding agent (Claude Code, Codex, OpenCode).

## Try it

```bash
pip install verdict-core
verdict quickstart --non-interactive --dry-run
```

Then [unknown ≠ healthy](unknown-not-healthy.md).

## How Verdict differs

Verdict is admission control, not a gateway. It decides whether a model may be used at all,
and proves that decision after the fact. Retries stay inside the admitted set: the relay may try
at most 3 gate-admitted alternatives, only when the request is safe to retry (ADR-016), and
orchestration recovery reassigns a failed node within its own eligible pool. Controller and worker
launches start from one canonical live admission set; the relay's live-admission boundary is
described in [docs/CONFIGURATION.md](../CONFIGURATION.md#admission-boundary).

| Design area | Verdict behavior | Comparison boundary |
|---|---|---|
| Named reasons for dropped candidates | Eight-code vocabulary (`policy`, `health`, `capability`, `quota`, `stale`, `opaque_mix`, `cost`, `unclassified`) in [`verdict/live_routing.py`](../../verdict/live_routing.py) | Compare other systems against their own documented behavior |
| Eligibility ladder with a recorded per-stage reason | Implemented in [`verdict/orchestration/eligibility.py`](../../verdict/orchestration/eligibility.py) | No competitor-wide absence asserted |
| Cheaper-first on `RouteSelection` paths | Constructor assertion in [`verdict/live_routing.py`](../../verdict/live_routing.py); not universal legacy catalog routing | No competitor-wide cost-invariant claim |
| Event-log digest against a retained receipt | [`verdict/orchestration/receipt.py`](../../verdict/orchestration/receipt.py); not signed or externally anchored | No competitor-wide absence asserted |
| Reviewer exclusion from final contributing implementer routes | [`verdict/orchestration/runtime.py`](../../verdict/orchestration/runtime.py); recorded PASS alone may lack raw non-skipped coverage | No competitor-wide absence asserted |
| Fallback / retry chains across providers | **Bounded, admitted-only.** The relay tries at most 3 gate-admitted alternatives on a retryable failure ([`verdict/relay.py`](../../verdict/relay.py) `build_attempts`, ADR-016); orchestration recovery reassigns within the node's eligible pool ([`verdict/orchestration/recovery.py`](../../verdict/orchestration/recovery.py)). There is no configured static fallback chain, and an excluded model is never tried | Other products need dated documentation for a feature-by-feature comparison |
| Load balancing across providers | No request-level load balancing. The orchestration selector spreads probes round-robin across providers within a capacity tier and caps concurrent nodes per route | No claim about specific gateway implementations |
| OpenTelemetry tracing | **Optional.** `verdict-core[tracing]` + `VERDICT_TRACING=1` enables OTLP span export. Metadata keys exclude prompt/header/key fields; producers must sanitize free-text `detail` values. | Available gateway support varies |
| Multi-provider inventory / transport | Delegated to OmniRoute (`verdict/omniroute.py`) — Verdict is transport-and-inventory-agnostic on purpose, not a from-scratch gateway | Gateway capabilities vary by product/version |

**Three explicit non-goals, stated rather than left ambiguous:**

- **No static fallback chains, on purpose.** Retries only ever use gate-admitted candidates:
  the relay tries at most 3 admitted alternatives when the request is safe to retry, and
  [`verdict/orchestration/recovery.py`](../../verdict/orchestration/recovery.py) reassigns within the
  same node's eligible pool. Verdict does not walk a configured list of providers hoping one answers.
- **OpenTelemetry is optional.** Install `verdict-core[tracing]` and set `VERDICT_TRACING=1`.
  See `verdict/tracing.py`.
- **Metadata authority differs by path.** Verdict owns an independent metadata store
  ([ADR-032](../adr/ADR-032-core-model-metadata-store.md)) on the metadata-aware single-route
  path. Orchestration currently reads capability, context-window and pricing fields from
  gateway `/v1/models` inventory; it does not enrich those rows from the metadata store.
