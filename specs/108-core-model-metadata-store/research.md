# Research: BOD-108 metadata sources

Copied from the Researcher source map on BOD-108 (2026-09-18). OmniRoute is
**not** a source.

## P0 — capability facts (hard gates)

| Source | URL | Fields | Notes |
|--------|-----|--------|-------|
| models.dev (primary) | `https://models.dev/api.json` (+ `models.json`) | `tool_call`, `structured_output`, `reasoning`, `attachment`, `modalities.input/output`, `limit.context/input/output`, pricing | Free unauth GET. Best SoT for tools/vision/context. |
| LiteLLM (secondary) | GitHub `model_prices_and_context_window.json` | context window, max output, function calling, vision, response schema, price | Fill only when models.dev left the field unknown. |

## P1 — quality / worthiness (nullable)

| Source | URL | Fields | Why stubbed in this PR |
|--------|-----|--------|------------------------|
| Artificial Analysis | `https://artificialanalysis.ai/api/v2/language/models/free` | intelligence / coding / **agentic**, TTFT | API key, ~100 req/day; not a core secret. Parser accepts fixtures. |
| Arena Elo | HF `lmarena-ai/leaderboard-dataset` | Elo/rank | No stable public API; would need `huggingface_hub`. |
| Open LLM Leaderboard | HF `open-llm-leaderboard/contents` | IFEval, BBH, … | Open weights mainly; extra dependency. |
| BFCL | HF `gorilla-llm/Berkeley-Function-Calling-Leaderboard` | tool-calling accuracy | CSV/dataset, not a JSON API. Binary `tools` still from models.dev. |

## Join key

Normalize OmniRoute inventory id → models.dev id via
`verdict/data/omniroute_models_dev_map.json`. Unlisted = `unmapped` named drop.
Do not fuzzy-match or substitute a related model.

## Invent-never

Never invent agentic/Elo/BFCL when the source was skipped. Receipts cite
`source` + `version|fetched_at` per used cap.
