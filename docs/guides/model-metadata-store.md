# Core model metadata store

Core owns an independently fetched **model metadata store**. OmniRoute is
inventory, execute, and health only — never the metadata source of truth.

Ticket: [BOD-108](https://linear.app/bodanglin/issue/BOD-108/shipp0-core-model-metadata-store-from-free-public-sources).
Capability-gate wiring into admit is [BOD-100](https://linear.app/bodanglin/issue/BOD-100/shipp0-capability-hard-gate-requirements-omniroute-resolved-metadata)
and is out of scope here; this slice ships the store, fetchers, mapping, and a
read API (`lookup_omniroute_id`) that later receipts can cite.

## Sources

| Priority | Source | Role | URL |
|---|---|---|---|
| P0 primary | models.dev | tools, vision, structured, reasoning, attachment, context | `https://models.dev/api.json` and `https://models.dev/models.json` |
| P0 secondary | LiteLLM | context/price/tool/vision/structured **fill** when models.dev left the field unknown | `model_prices_and_context_window.json` on GitHub |
| P1 stub | Artificial Analysis | intelligence / coding / **agentic** / latency — stored only if a payload is supplied | free API requires a key (~100 req/day) |
| P1 stub | Arena Elo | Elo — Hugging Face `lmarena-ai/leaderboard-dataset` | no core `huggingface_hub` dependency |
| P1 stub | Open LLM Leaderboard | open-weight scores — HF `open-llm-leaderboard/contents` | same |
| P1 stub | BFCL | tool-calling **quality** (binary `tools` still from models.dev) | HF CSVs, no stable JSON API |

Refresh:

```bash
verdict metadata refresh --json
verdict metadata refresh --models-dev-api-file … --litellm-file … --json   # offline
verdict metadata lookup groq/llama-3.3-70b-versatile --requires tools,vision --json
```

The on-disk snapshot defaults to `~/.verdict/model-metadata.json`.

## Provenance rules

Every stored field is a `{value, provenance}` object. Provenance **must** cite
`source` and at least one of `version` or `fetched_at`. Optional `raw_id` is the
source-native identifier. A later receipt should call
`MetadataLookup.provenance_for_receipt()` so each used cap names its source.

LiteLLM never overrides models.dev. A disagreement is recorded on the snapshot
as a conflict; the models.dev value is kept.

## Invent-never

- Soft ranks (`aa_agentic`, Arena Elo, BFCL, Open LLM) are stored **only** when
  a fetcher actually returned them. Missing P1 sources are `skipped` with a
  documented reason — the fields stay null.
- Unmapped OmniRoute ids, map targets missing from models.dev, and required
  fields that were not fetched are **named drops** (`unmapped`,
  `map_target_missing`, `required_unknown`). They are not guessed.
- The OmniRoute → models.dev join is an **explicit map**
  (`verdict/data/omniroute_models_dev_map.json`) plus exact identity equality.
  Related-model substitution is forbidden.
- Absence of a price is not evidence the model is free (see ADR-027).
