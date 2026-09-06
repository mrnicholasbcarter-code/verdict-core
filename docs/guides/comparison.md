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

- We do not claim a percent cost cut here. Demo estimates live in the README with a caveat; they are not a comparison metric.
- We do not claim more models than OpenRouter. Catalog size is theirs. Qualification is ours.
- We do not claim to be a coding agent (Claude Code, Codex, OpenCode).

## Try it

```bash
pip install verdict-core
verdict quickstart --non-interactive --dry-run
```

Then [unknown ≠ healthy](unknown-not-healthy.md).
