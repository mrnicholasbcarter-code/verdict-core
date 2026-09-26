# Core model metadata store

Core owns an independently fetched **model metadata store**. OmniRoute is
inventory, execute, and health only — never the metadata source of truth.
OpenRouter catalog endpoints are likewise **not** a metadata source of truth
(independence + ToS).

Ticket: the core model metadata store (ADR-032)
(store). Identity join follow-up: the models.json id-join fix.
Capability-gate wiring into admit is the spend-preservation gate design;
this guide covers the store, fetchers, mapping/join, and the read API
(`lookup_omniroute_id`) that receipts cite.

## Sources (ranked)

| Priority | Source | Role | URL |
|---|---|---|---|
| P0 primary (canonical join) | models.dev **models.json** | Provider-agnostic lab/model facts: tools, vision, structured, context, reasoning, attachment | `https://models.dev/models.json` — field provenance `source=models.dev.models` |
| P0 primary (provider catalog) | models.dev **api.json** | Provider-scoped rows; fills ids not present in models.json | `https://models.dev/api.json` — field provenance `source=models.dev` |
| P0 secondary | LiteLLM | context/price/tool/vision/structured **fill** when models.dev left the field unknown | `model_prices_and_context_window.json` on GitHub |
| P1 stub | Artificial Analysis | intelligence / coding / **agentic** / latency — stored only if a payload is supplied | free API requires a key (~100 req/day) |
| P1 stub | Arena Elo | Elo — Hugging Face `lmarena-ai/leaderboard-dataset` | no core `huggingface_hub` dependency |
| P1 stub | Open LLM Leaderboard | open-weight scores — HF `open-llm-leaderboard/contents` | same |
| P1 stub | BFCL | tool-calling **quality** (binary `tools` still from models.dev) | HF CSVs, no stable JSON API |

**Non-sources of truth:** OmniRoute `/v1/models`, OpenRouter `/api/v1/models`, and any
gateway inventory payload. Those are execute/inventory only.

Refresh:

```bash
verdict metadata refresh --json
verdict metadata refresh --models-dev-api-file … --models-dev-models-file … --litellm-file … --json
verdict metadata lookup agy/gemini-3.1-flash-lite --requires tools --json
```

The on-disk snapshot defaults to `~/.verdict/model-metadata.json`.

## Join rules

`lookup_omniroute_id` resolves a gateway/provider inventory id in this order:

1. **Exact map** — `verdict/data/omniroute_models_dev_map.json` (explicit aliases only).
2. **Exact store id** — inventory id equals a stored models.dev id (or an
   OmniRoute alias already attached at refresh).
3. **Unique leaf in models.json** — leaf = segment after the last `/` (or the
   full id if unscoped). Match only against records whose caps cite
   `source=models.dev.models`. Exactly one hit → join. Zero hits → named drop
   `unmapped`. Two or more hits → named drop `unmapped` (ambiguous; never an
   arbitrary pick).
4. Else **`unmapped`**.

Invent-never for variants: effort/variant suffixes without a models.json row
(e.g. `agy/gemini-2.0-flash-high`) are **not** silently stripped to a base leaf
unless an explicit, tested map entry says so. Result is a named drop
(`unmapped` / `required_unknown` as applicable).

Prefer unique-leaf over growing the explicit map when the leaf is unique in
models.json (e.g. `agy/gemini-3.1-flash-lite` → `google/gemini-3.1-flash-lite`).

## Provenance rules

Every stored field is a `{value, provenance}` object. Provenance **must** cite
`source` and at least one of `version` or `fetched_at`. Optional `raw_id` is the
source-native identifier. A later receipt should call
`MetadataLookup.provenance_for_receipt()` so each used cap names its source
(models.json matches cite `models.dev.models`, not OmniRoute).

LiteLLM never overrides models.dev. A disagreement is recorded on the snapshot
as a conflict; the models.dev value is kept. On id overlap between api.json and
models.json, **models.json wins**.

## Invent-never

- Soft ranks (`aa_agentic`, Arena Elo, BFCL, Open LLM) are stored **only** when
  a fetcher actually returned them. Missing P1 sources are `skipped` with a
  documented reason — the fields stay null.
- Unmapped OmniRoute ids, map targets missing from models.dev, and required
  fields that were not fetched are **named drops** (`unmapped`,
  `map_target_missing`, `required_unknown`). They are not guessed.
- Related-model substitution and silent suffix-stripping are forbidden.
- Absence of a price is not evidence the model is free (see ADR-027).
