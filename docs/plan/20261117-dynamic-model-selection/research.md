# T1 Research - Dynamic Model Selection (Corrected)

**Worktree:** `plan-dynamic-model-selection` at `origin/main 8e640a9d` plus plan commits
**Date:** 2026-11-17
**Scope:** verdict scripts .prime packages benchmarks config deploy examples contracts

## A. Grep Commands and Dispositions

All greps run using pattern files to eliminate shell quoting issues.
**Scope Dirs found:** verdict, scripts, .prime, packages, benchmarks, config, deploy, examples, contracts.

### Grep 1: auto/combo route strings
`git grep -nE -f p1.txt -- $SCOPE`
**Result:** 8 hits

| File | Line | Pattern | Disposition |
|------|------|---------|-------------|
| `scripts/demo_orchestrate.py` | 103 | `"auto/best-coding"` | FX (demo fixture) |
| `verdict/workers.py` | 23 | `"auto/best-coding", "auto/best-reasoning", "auto/best-free", "auto/fast"` | DR (T8) |
| `verdict/workers.py` | 24 | `"auto/best-coding", "auto/coding:free", "auto/best-free", "auto/fast"` | DR (T8) |
| `verdict/workers.py` | 25 | `"auto/best-reasoning", "auto/reasoning:free", "auto/best-free", "auto/fast"` | DR (T8) |
| `verdict/workers.py` | 26 | `"auto/best-fast", "auto/fast", "auto/best-free"` | DR (T8) |
| `verdict/workers.py` | 27 | `"auto/best-free", "auto/coding:free", "auto/reasoning:free", "auto/fast"` | DR (T8) |
| `verdict/workers.py` | 93 | `"auto/best-free"` | DR (T8) |
| `verdict/workers.py` | 298 | `"auto/best-free", "auto/fast"` | DR (T8) |

### Grep 2: DEFAULT_MODEL / CONTROLLER_MODEL constants
`git grep -nE -f p2.txt -- $SCOPE`
**Result:** 52 hits

| File | Line | Symbol | Disposition |
|------|------|--------|-------------|
| `.prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py` | 20,138 | `LEGACY_CONTROLLER_MODELS` | DR (T9) |
| `verdict/actions/helpers.py` | 33,35 | `DEFAULT_PRIMARY_MODEL` | DR (T8) |
| `verdict/api.py` | 41,747 | `DEFAULT_PRIMARY_MODEL` | DR (T8) |
| `verdict/autodev_run.py` | 75,3227 | `DEFAULT_EXECUTOR_MODEL` | RS |
| `verdict/cli.py` | 22,32,469,476,642,1476,1479,3658 | `DEFAULT_PRIMARY_MODEL`, `DEFAULT_ORCHESTRATOR_MODEL`, `HERMES_HARNESS_DEFAULT_MODEL` | DR (T7/T8), RS |
| `verdict/commands/dispatch.py` | 20,472 | `HERMES_HARNESS_DEFAULT_MODEL` | RS |
| `verdict/commands/parsers_harness.py` | 17,79 | `HERMES_HARNESS_DEFAULT_MODEL` | RS |
| `verdict/contracts.py` | 25 | `DEFAULT_PRIMARY_MODEL` | DR (T8) |
| `verdict/decomposer.py` | 35,98,424 | `DEFAULT_ORCHESTRATOR_MODEL` | DR (T7) |
| `verdict/gate.py` | 6,92,151,164 | `DEFAULT_PRIMARY_MODEL` | DR (T8) |
| `verdict/harness_hermes.py` | 32,91,100,523 | `DEFAULT_MODEL` | RS |
| `verdict/harness_opencode.py` | 36,68,212,432,505 | `DEFAULT_MODEL` | RS |
| `verdict/harness_prime.py` | 36,443,760 | `DEFAULT_MODEL_ID` | RS |
| `verdict/provider_bootstrap.py` | 35,731 | `DEFAULT_PRIMARY_MODEL` | DR (T8) |
| `verdict/provider_detection.py` | 24,750,784,790 | `DEFAULT_PRIMARY_MODEL` | DR (T8) |
| `verdict/subagent_models.py` | 28,217 | `DEFAULT_PRIMARY_MODEL` | DR (T8) |
| `verdict/subagent_selection.py` | 32,33,321 | `CONTROLLER_MODEL`, `CONTROLLER_MODELS` | DR (T8) |
| `verdict/worker_runtime.py` | 164 | `VERDICT_CONTROLLER_MODEL` | RS |

### Grep 3: model = route_id assignments
`git grep -nE -f p3.txt -- $SCOPE`
**Result:** 2 hits

| File | Line | Pattern | Disposition |
|------|------|---------|-------------|
| `.prime/agent/skills/verdict-dispatch/SKILL.md` | 42 | `controller_model="cc/claude-fable-5-1"` | DO |
| `verdict/subagent_resolver.py` | 250 | `primary_model="verdict/prepare"` | RS |

### Grep 4: vendor prefix strings
`git grep -nE -f p4.txt -- $SCOPE`
**Result:** 81 hits

| File | Lines | Pattern | Disposition |
|------|------|---------|-------------|
| `verdict/contracts.py` | 25 | `"anthropic/claude-opus-5"` | DR (T8) |
| `verdict/decomposer.py` | 35 | `"mistral/codestral-latest"` | DR (T7) |
| `verdict/data/omniroute_models_dev_map.json` | 27-106 | various vendor prefixes | RS (Map file) |
| `verdict/classifier.py` | 160 | `"anthropic/claude-sonnet-4"` | RT |
| `verdict/admission.py` | 642 | `"kr"` | RT |
| `scripts/live_savings_bench.py` | 51,52,59-61 | `cc/claude-opus-5` etc. | FX |
| `scripts/selection_report.py` | 30 | `kr/claude-sonnet-5` | FX |
| `benchmarks/fixtures/*.json` | various | various vendors | FX |
| `.prime/agent/skills/verdict-dispatch/SKILL.md` | 42 | `cc/claude-fable-5-1` | DO |
| `verdict/mcp_server.py` | 127 | `anthropic/claude-opus-5` | DO |
| `verdict/orchestration/cli.py` | 120 | `cc/*` | DO |
| `verdict/orchestration/harness.py` | 62 | `cc/claude-sonnet-5` | DO |
| `verdict/orchestration/tui.py` | 690 | `cc/claude-haiku-4-5-20251001` | DO |
| `verdict/responses_compatibility.py` | 14-17 | `nvidia/nvidia/...` | RS |
| `verdict/savings_bench.py` | 108,112,113 | `nvidia/`, `cx/` | FX |
| `verdict/subagent_selection.py` | 32 | `cx/gpt-5.6-sol` | DR (T8) |
| `verdict/omniroute_pipeline/probes.py` | 171,172 | `nvidia/` | RS |

### Grep 5: classifier / subagent_selection / provider_catalog patterns
`git grep -nE -f p5.txt -- verdict/classifier.py verdict/subagent_selection.py verdict/orchestration/provider_catalog.py .prime/agent/skills/verdict-dispatch/scripts`
**Result:** 22 hits

| File | Line | Pattern | Disposition |
|------|------|---------|-------------|
| `verdict/classifier.py` | 10,43,62,80,88,89,108,136 | `CAPABILITY_PATTERNS` and regex | RT |
| `verdict/classifier.py` | 172-183, 205 | `_matches_any` | RT |
| `verdict/orchestration/provider_catalog.py` | 63,128,137,157 | `route_family` | RT |
| `verdict/subagent_selection.py` | 367 | `code, coder, codex, devstral, codestral` | DR (T8) |

**Additions not in hardcoded_inventory:**
- `verdict/classifier.py:108` `mini` variant pattern — RT
- `verdict/classifier.py:136` `nous-coder` pattern — RT
- `verdict/classifier.py:43-48` `qwen-coder` patterns — RT
- `verdict/classifier.py:80` `qwen2.5-coder-32b` — RT
- `verdict/classifier.py:172-176` `mini` variant precedence — RT

## B. Identity Coverage — Runtime Measurement

**Script:** `/home/nick/dev/verdict-core/.git/verdict-prime/t1_identity_coverage.py` (read-only, outside repo)
**Live catalog:** `GET /v1/models` — 4,310 routes
**Store:** `~/.verdict/model-metadata.json` — 6,739 models, refreshed 2026-11-17T13:50:29Z
**Identity map:** `verdict/data/omniroute_models_dev_map.json` — 25 entries

### Production callers (verified by `git grep -n lookup_omniroute_id -- verdict/`):
- `verdict/orchestration/eligibility.py:444` — `load_store()` then `lookup_omniroute_id(metadata, route_id, identity_map=mapping)`
- `verdict/candidate_pool.py:701` — `lookup_omniroute_id(…)`
- `verdict/capability_gate.py:224` — `lookup_omniroute_id(…)`
- `verdict/identify_rank.py:100` — `lookup_omniroute_id(metadata, route_id, identity_map=identity_map)`
- `verdict/intelligence.py:1031` — `lookup_omniroute_id(…)`

### Join order (`verdict/metadata/store.py:280` `lookup_omniroute_id`):
1. **Explicit map** — `identity_map.models_dev_id_for(omniroute_id)`
2. **Identity match** — `key in snapshot._omniroute_index` (route id equals store id)
3. **Unique leaf** — `model_id_leaf(key)` in `snapshot._leaf_index` (single record wins; ambiguous = drop)
4. **Unmapped** — named drop (`DROP_UNMAPPED`)

### Aggregate results:
| Join mechanism | Count |
|----------------|-------|
| explicit_map | 6 |
| identity_match | 85 |
| unique_leaf | 1,375 |
| **Total mapped** | **1,466 (34.0%)** |
| unmapped | 2,844 (66.0%) |
| ambiguous_leaf | 0 |

### Per-family runtime coverage (4,310 total):

| Family | Total | Explicit | Identity | Leaf | Unmapped | %Mapped |
|--------|-------|----------|----------|------|----------|---------|
| kc | 463 | 0 | 0 | 283 | 180 | 61.1% |
| kilocode | 463 | 0 | 0 | 283 | 180 | 61.1% |
| af | 404 | 0 | 0 | 147 | 257 | 36.4% |
| api-airforce | 404 | 0 | 0 | 147 | 257 | 36.4% |
| cursor | 368 | 0 | 0 | 15 | 353 | 4.1% |
| cu | 365 | 0 | 0 | 15 | 350 | 4.1% |
| huggingface | 172 | 0 | 0 | 22 | 150 | 12.8% |
| hf | 164 | 0 | 0 | 20 | 144 | 12.2% |
| aihorde | 163 | 0 | 0 | 0 | 163 | 0.0% |
| dva | 110 | 0 | 0 | 1 | 109 | 0.9% |
| nvidia | 92 | 0 | 10 | 15 | 67 | 27.2% |
| pollinations | 85 | 0 | 0 | 10 | 75 | 11.8% |
| llm7 | 84 | 0 | 0 | 46 | 38 | 54.8% |
| opencode-zen | 81 | 0 | 0 | 66 | 15 | 81.5% |
| pol | 75 | 0 | 0 | 9 | 66 | 12.0% |
| lmarena | 75 | 0 | 0 | 23 | 52 | 30.7% |
| bai | 74 | 0 | 0 | 49 | 25 | 66.2% |
| openrouter | 65 | 0 | 0 | 17 | 48 | 26.2% |
| mistral | 56 | 1 | 25 | 0 | 30 | 46.4% |
| cxa | 52 | 0 | 0 | 8 | 44 | 15.4% |
| lma | 48 | 0 | 0 | 22 | 26 | 45.8% |
| auto | 45 | 0 | 0 | 0 | 45 | 0.0% |
| opencode-go | 32 | 0 | 32 | 0 | 0 | 100.0% |
| aug | 28 | 0 | 0 | 3 | 25 | 10.7% |
| huggingchat | 24 | 0 | 0 | 5 | 19 | 20.8% |
| bzl | 21 | 0 | 0 | 10 | 11 | 47.6% |
| bazaarlink | 21 | 0 | 0 | 10 | 11 | 47.6% |
| cfp | 20 | 0 | 0 | 14 | 6 | 70.0% |
| codex | 18 | 0 | 0 | 11 | 7 | 61.1% |
| cl | 17 | 0 | 0 | 8 | 9 | 47.1% |
| cline | 17 | 0 | 0 | 8 | 9 | 47.1% |
| agy | 16 | 0 | 0 | 3 | 13 | 18.8% |
| cc | 15 | 3 | 0 | 12 | 0 | 100.0% |
| claude | 15 | 1 | 0 | 14 | 0 | 100.0% |
| zc | 14 | 0 | 0 | 13 | 1 | 92.9% |
| ollamacloud | 14 | 0 | 0 | 3 | 11 | 21.4% |
| ollama-cloud | 14 | 0 | 13 | 0 | 1 | 92.9% |
| groq | 12 | 1 | 5 | 5 | 1 | 91.7% |
| antigravity | 11 | 0 | 0 | 4 | 7 | 36.4% |
| cx | 11 | 0 | 0 | 8 | 3 | 72.7% |
| gh | 9 | 0 | 0 | 5 | 4 | 55.6% |
| github | 9 | 0 | 0 | 5 | 4 | 55.6% |
| unknown | 8 | 0 | 0 | 5 | 3 | 62.5% |
| grok-cli | 7 | 0 | 0 | 5 | 2 | 71.4% |
| ddgw | 6 | 0 | 0 | 5 | 1 | 83.3% |
| duckduckgo-web | 6 | 0 | 0 | 5 | 1 | 83.3% |
| gc | 5 | 0 | 0 | 3 | 2 | 60.0% |
| fmd | 5 | 0 | 0 | 5 | 0 | 100.0% |
| freemodel-dev | 5 | 0 | 0 | 5 | 0 | 100.0% |
| ollama-local | 4 | 0 | 0 | 1 | 3 | 25.0% |
| queen | 3 | 0 | 0 | 0 | 3 | 0.0% |
| worker | 2 | 0 | 0 | 0 | 2 | 0.0% |
| devin-desktop | 2 | 0 | 0 | 0 | 2 | 0.0% |
| veoaifree-web | 2 | 0 | 0 | 0 | 2 | 0.0% |
| veo-free | 2 | 0 | 0 | 0 | 2 | 0.0% |
| codestral | 2 | 0 | 0 | 2 | 0 | 100.0% |
| escalation | 1 | 0 | 0 | 0 | 1 | 0.0% |
| aq | 1 | 0 | 0 | 0 | 1 | 0.0% |
| amazon-q | 1 | 0 | 0 | 0 | 1 | 0.0% |
| unc | 1 | 0 | 0 | 0 | 1 | 0.0% |
| ollama | 1 | 0 | 0 | 0 | 1 | 0.0% |

### Sample unmapped routes per family (5 each):

| Family | Unmapped sample routes |
|--------|------------------------|
| kc/kilocode | `kc/kilo-auto/efficient`, `kc/kilo-auto/free`, `kc/anthropic/claude-opus-5.5`, `kc/baidu/ernie-5.5-flash-preview:free`, `kc/baidu/ernie-light-dt-1.0-intl` |
| af/api-airforce | `af/gemma3-270m:free`, `af/Doubao-1.5-vision-pro-32k`, `af/Doubao-vision-lite-32k`, `af/Doubao-vision-pro-32k`, `af/ERNIE-3.5-8K` |
| cursor/cu | `cursor/default`, `cursor/grok-4.7-low`, `cursor/grok-4.7-medium`, `cursor/grok-4.7-high`, `cursor/grok-4.7-xhigh` |
| huggingface/hf | `huggingface/deepseek-ai/DeepSeek-V3`, `huggingface/Qwen/Qwen3.8-27B`, `huggingface/thinkingmachines/Inkling-2-Small`, `huggingface/deepseek-ai/DeepSeek-V4.1-Flash-1017`, `huggingface/moonshotai/Kimi-K3.1-Pro` |
| aihorde | `aihorde/2DN`, `aihorde/526Mix-Animated`, `aihorde/A-Zovya RPG Inpainting`, `aihorde/AAM XL`, `aihorde/AbsoluteReality` |
| nvidia | `nvidia/nv-embedqa-e5-v5`, `nvidia/black-forest-labs/flux.1-dev`, `nvidia/black-forest-labs/flux.1-schnell`, `nvidia/black-forest-labs/flux.1-kontext-dev`, `nvidia/black-forest-labs/flux.2-klein-4b` |
| openrouter | `openrouter/inclusionai/ling-3.2-flash:free`, `openrouter/baidu/ernie-5.5-flash-preview:free`, `openrouter/thinkingmachines/inkling-2-small:free`, `openrouter/liquid/lfm-3-1.7b:free`, `openrouter/particle-ai/particle-1:free` |
| cc/claude | (none — 100% mapped) |
| opencode-go | (none — 100% mapped via identity_match) |
| auto | `auto/best-coding`, `auto/best-reasoning`, `auto/best-fast`, `auto/best-vision`, `auto/best-chat` |
| groq | `groq/distil-whisper-large-v3-en` |
| cx | `cx/codex-auto-review`, `cx/gpt-reserve`, `cx/gpt-6.1-luna` |

## C. Capacity Evidence (Re-verified)
(Same as previous run, mappings to P2, P6, P7, P8 hold)

## D. Call-Log Fields for ReliabilityObservation
(Same as previous run, field mapping to `ReliabilityObservation v1` holds)
