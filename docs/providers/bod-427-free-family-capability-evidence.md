# BOD-427: capability evidence for the leftover free-model families

Status: implemented in `verdict/classifier.py` (`CAPABILITY_PATTERNS`, rows
marked `BOD-427`). Guarded by `tests/test_classifier_free_families_bod427.py`.

## Scope and inputs

- Free census: OmniRoute free-tier summary `perModel`, as cached in
  `~/.verdict/admit-last-good.json` (fetched 2026-10-17T12:14:47Z, 489 free
  ids). `free_tier_admit.py` sets `capability_tier=classify(model_id)` on
  admitted rows, and the orchestration ladder drops `classify_known(...) is
  None` rows as `unknown_capability` whenever `max_capability_tier < 3`.
- On origin/main d9992045 (after BOD-419 / #907), 127 of the 489 free ids
  were still unknown. This doc covers every one of them: 80 ids now get a
  tier, 47 stay unknown on purpose.
- No re-tiers: every id `classify_known` knew on main (7,235 of the 10,050
  ids in the free census, `catalog-cache.json` and
  `census-catalog-cache.json`) is pinned in
  `tests/fixtures/catalog_truth/classifier_main_tiers_bod427.json`, and the
  regression test asserts the exact same tier.

## Tier semantics used (from `verdict/classifier.py`)

| Tier | Meaning in the classifier | Existing reference rows |
| --- | --- | --- |
| 0 | Most capable; also the frontier spend guard (`eligibility.py` `_task_gate`) | opus, gpt-5.5/5.6, gpt-6-sol, grok-4, o3 |
| 1 | Strong near-frontier work models | sonnet-4/5, gpt-5.4, gpt-4o, deepseek-r1, qwen-235b, gemini-pro, glm-4.5+/5, kimi-k2/k3, deepseek-v4, minimax-m2/m3 |
| 2 | Mid tier | sonnet-3, gpt-4o-mini, llama-70b, qwen-72b/32b, mistral-large, deepseek-v3, command-r-plus, gpt-oss-120b |
| 3 | Cheapest/fastest, only for low-criticality work | haiku, flash, mini, nano, lite, 7b/8b, mistral-small, qwen-14b, small-active MoE (a3b/a4b) |

Rules applied:

- Tier 0 is the spend guard. As in BOD-419, no free family is moved there,
  even when the vendor or an index calls it frontier-class (open question 1).
- A family is placed next to the existing row it is most comparable to on
  the cited evidence (size, active parameters, benchmark parity).
- Where the only evidence is 2024-era vendor claims or Chinese-only
  benchmarks with no current independent placement, the family gets the
  most conservative tier (3) and the row says so.
- Routers, pools, stealth routes, unversioned vendor aliases and
  special-purpose models (apply-only, theorem prover, safety moderator)
  stay unknown (`None`), as BOD-419 did for alias rows.
- Explicit cheap markers win: `gpt-4.1-mini`, `aion-3.0-mini`,
  `Llama-4-Maverick-17B-…` keep tier 3.

## Evidence table: classified families

| Family | Example free ids | Tier | Evidence URL(s) | Key fact used |
| --- | --- | --- | --- | --- |
| Claude 3.7 Sonnet | `api-airforce/anthropic/claude-3.7-sonnet`, `coze/claude-3-7-sonnet-20250514`, `t3-web/claude-3.7` | 1 | https://www.anthropic.com/news/claude-3-7-sonnet | Successor to Claude 3.5 Sonnet, which is already tier 1. SWE-bench Verified 62.3% (70.3% with scaffold). The coze date suffix is not the 3.7 release date; either reading is tier 1. |
| Claude Sonnet 4 (vendor-order alias) | `deepinfra/anthropic/claude-4-sonnet` | 1 | https://www.anthropic.com/news/claude-4 | Same model as `claude-sonnet-4` (tier 1 via `sonnet-4`). SWE-bench Verified 72.7%. |
| GPT-4.1 | `bluesminds/gpt-4.1`, `t3-web/gpt-4.1` | 1 | https://openai.com/index/gpt-4-1/ | SWE-bench Verified 54.6% vs 33.2% for GPT-4o, which is tier 1. `-mini`/`-nano` stay tier 3. |
| GPT-5 (base) | `t3-web/gpt-5` | 1 | https://openai.com/index/introducing-gpt-5/ | Predates gpt-5.4 (tier 1). Tier 0 begins at gpt-5.5. |
| GPT-5.3-Codex | `freemodel-dev/gpt-5.3-codex` | 1 | https://openai.com/index/introducing-gpt-5-3-codex/ | Feb 2026 agentic coder: SWE-Bench Pro 56.8%, Terminal-Bench 2.0 77.3%. Predates gpt-5.4 (tier 1). |
| Xiaomi MiMo-V2.5-Pro | `bazaarlink/mimo-v2.5-pro`, `deepinfra/XiaomiMiMo/MiMo-V2.5-Pro`, `huggingchat/XiaomiMiMo/MiMo-V2.5-Pro` | 1 | https://huggingface.co/XiaomiMiMo/MiMo-V2.5-Pro ; https://mimo.mi.com/docs/en-US/updates/model | 1.02T total / 42B active MoE. SWE-bench Verified 78.9 (FP8 card). Vendor says it is comparable to Claude Opus 4.6 on agent workloads. Base MMLU-Pro 68.5 vs Kimi-K2 69.2, which is tier 1. |
| inclusionAI Ling-2.6-1T | `huggingchat/inclusionAI/Ling-2.6-1T`, `opencode/ling-2.6-1t-free` | 1 | https://huggingface.co/inclusionAI/Ling-2.6-1T ; https://developer.ant-ling.com/en/docs/models/ling/ | About 1T total / 63B active flagship. Vendor claims open-source SOTA on SWE-bench Verified, BFCL-V4 and TAU2-Bench. |
| Meituan LongCat-2.0 | `longcat/LongCat-2.0` | 1 | https://longcat.ai/blog/longcat-2.0/ ; https://github.com/meituan-longcat/longcat-2.0 | 1.6T total / ~48B active MoE, post-trained for agentic coding, 1M context (Jun 2026). |
| NVIDIA Nemotron 3 Ultra | `huggingchat/nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4`, `kilo-gateway/nvidia/nemotron-3-ultra-550b-a55b:free`, `opencode-zen/opencode/nemotron-3-ultra-free` | 1 | https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b/modelcard | 550B total / 55B active. NVIDIA positions it for "frontier reasoning, complex agentic workflows". Tier 1, not 0 (spend guard). |
| Tencent Hy3 | `kilo-gateway/tencent/hy3:free` | 1 | https://huggingface.co/tencent/Hy3 ; https://venturebeat.com/technology/tencents-apache-licensed-hy3-takes-on-glm-5-2-at-half-the-size-and-wins-everywhere-except-coding | 295B total / 21B active (Jul 2026). SWE-bench Verified 78.0%. Competes with GLM-5.2, which is tier 1. |
| Kwaipilot KAT-Coder-Pro | `kilo-gateway/kwaipilot/kat-coder-pro-v2.5:free` | 1 | https://arxiv.org/html/2607.05471 ; https://openrouter.ai/kwaipilot/kat-coder-pro-v2.5 | Agentic repo coder: SWE-Bench Pro 65.2 and PinchBench 94.9 in the technical report. |
| Poolside Laguna M.1 | `kilo-gateway/poolside/laguna-m.1:free`, `routeway/laguna-m.1:free` | 1 | https://docs.poolside.ai/get-started/supported-models ; https://benchlm.ai/compare/laguna-m-1-vs-laguna-xs-2-1 | 225B total / 23B active. SWE-bench Verified 74.6% and SWE-bench Pro 49.2% (BenchLM), above Claude Sonnet 4's 72.7%. |
| Poolside Laguna S 2.1 | `nara/laguna-s-2.1` | 1 | https://poolside.ai/blog/introducing-laguna-s-2-1 ; https://docs.poolside.ai/get-started/supported-models | 118B total / 8B active, 1M context. SWE-bench Pro 59.4% and SWE-bench Multilingual 78.5% (vendor, mean pass@1). |
| Meta Muse Spark | `muse-spark-web/muse-spark`, `-thinking`, `-contemplating` | 1 | https://ai.meta.com/blog/introducing-muse-spark-msl/ ; https://artificialanalysis.ai/articles/muse-spark-1-3 | Meta Superintelligence Labs multimodal reasoning model (Apr 2026). Meta cites gaps in long-horizon agentic work and coding. Artificial Analysis: "Meta reaches the frontier" (1.3). Tier 1, not 0 (spend guard). |
| AI21 Jamba Large 1.7 | `ai21/jamba-large-1.7` | 2 | https://huggingface.co/ai21labs/AI21-Jamba-Large-1.7 ; https://artificialanalysis.ai/models/jamba-1-7-large | 398B total / 94B active hybrid SSM-Transformer MoE. Grouped with mistral-large (tier 2). |
| Llama 4 Maverick | `ainative/llama-4-maverick`, `bazaarlink/llama-4-maverick`, `t3-web/llama-4-maverick` | 2 | https://ai.meta.com/blog/llama-4-multimodal-intelligence/ | 400B total / 17B active, 128 experts. Meta claims GPT-4o parity, but independent results came in lower, so it is placed conservatively in the llama-70b class. Ids with an explicit `17b` stay tier 3 (main). |
| Arcee Trinity Large | `arcee-ai/arcee-ai/trinity-large-preview:free`, `opencode/trinity-large-preview-free` | 2 | https://huggingface.co/arcee-ai/Trinity-Large-Preview | 398B total / 13B active, "lightly post-trained". MMLU-Pro 75.2 vs Maverick 80.5, GPQA 63.3 vs 69.8, so Maverick class. |
| AionLabs Aion 2.x/3.0 | `aion/aion-labs/aion-2.0`, `aion-2.5`, `aion-3.0` | 2 | https://openrouter.ai/aion-labs/aion-2.0 ; https://openrouter.ai/aion-labs/aion-3.0 ; https://llmtr.com/models/aion-labs/aion-2.5 | 2.0 and 2.5 are DeepSeek V3.2-based roleplay models (deepseek-3.2 is tier 2). 3.0 is a GLM-based multi-model roleplay system, kept at tier 2 because it is not a coding or work model. `-mini` ids stay tier 3. |
| xAI Grok 2 | `api-airforce/x-ai/grok-2-1212` | 2 | https://x.ai/news/grok-2 | Aug 2024 predecessor of Grok 3 (tier 1). At launch it was ahead of GPT-4 Turbo and level with Claude 3.5 Sonnet on LMSYS. Older generation, so tier 2. |
| Xiaomi MiMo-V2.5 | `bazaarlink/mimo-v2.5`, `deepinfra/XiaomiMiMo/MiMo-V2.5`, `opencode-zen/opencode/mimo-v2.5-free` | 2 | https://huggingface.co/XiaomiMiMo/MiMo-V2.5-Pro | 310B total / 15B active. Base MMLU-Pro 65.8 vs 68.5 for Pro, GPQA 58.1 vs 66.7, so one step below Pro. |
| NVIDIA Nemotron 3 Super | `nvidia/nvidia/nemotron-3-super-120b-a12b`, `ollama-cloud/nemotron-3-super`, `opencode/nemotron-3-super-free` (+4) | 2 | https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/modelcard ; https://artificialanalysis.ai/models/comparisons/nvidia-nemotron-3-super-120b-a12b-vs-gpt-oss-120b | 120B total / 12B active. Artificial Analysis index 13 vs 12 for gpt-oss-120b, so the same class (tier 2). |
| Cohere Command A (+reasoning, vision) | `cohere/command-a-03-2025`, `cohere/command-a-reasoning-08-2025`, `huggingchat/CohereLabs/command-a-vision-07-2025` | 2 | https://cohere.com/blog/command-a ; https://artificialanalysis.ai/models/comparisons/command-a-vs-gpt-4o | 111B. Cohere claims GPT-4o / DeepSeek-V3 parity, but Artificial Analysis rates both higher, so deepseek-v3 class. command-r-plus is also tier 2. |
| Nous Hermes 3/4 (70B, 405B) | `deepinfra/NousResearch/Hermes-3-Llama-3.1-405B`, `nous-research/Hermes-4-405B`, `nous-research/Hermes-4-70B` | 2 | https://arxiv.org/abs/2508.18255 ; https://huggingface.co/NousResearch/Hermes-3-Llama-3.1-405B | Llama-3.1 finetunes. Hermes 4 405B (reasoning): GPQA 70.6, LiveCodeBench 61.4. `Hermes-3-Llama-3.1-70B` is already tier 2 via `llama.*70b`. |
| Qwen3.6 Plus (Fireworks spelling) | `fireworks/qwen3p6-plus` | 2 | https://fireworks.ai/models/fireworks/qwen3p6-plus | Same model as `qwen3.6-plus`, which is tier 2 via the BOD-419 qwen-plus row. Only the `3p6` spelling was missed. |
| Qwen-SEA-LION v4 32B | `publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT`, `sealion/aisingapore/Qwen-SEA-LION-v4-32B-IT` | 2 | https://huggingface.co/aisingapore/Qwen-SEA-LION-v4-32B-IT | Qwen3-32B continued-pretrain and instruct tune. qwen-32b is tier 2. |
| Ai2 Olmo 3 32B Think | `publicai/allenai/Olmo-3-32B-Think` | 2 | https://huggingface.co/allenai/Olmo-3-32B-Think ; https://allenai.org/blog/olmo3 | 32B reasoning model at Qwen3-32B parity: ahead on MATH, AIME25 and IFEval, behind on LiveCodeBench, GPQA and MMLU. |
| GPT-5.6 Luna | `duckduckgo-web/gpt-5.6-luna` | 3 | https://developers.openai.com/api/docs/models/gpt-5.6-luna ; https://developers.cloudflare.com/ai/models/openai/gpt-5.6-luna/ | OpenAI's "cost-sensitive, high-volume" tier at $0.20/$1.20 per M tokens, a cheap serving tier like `mini`. Main already excludes luna from tier-0 gpt-5.6. |
| Llama 4 Scout | `bazaarlink/llama-4-scout`, `t3-web/llama-4-scout` | 3 | https://ai.meta.com/blog/llama-4-multimodal-intelligence/ | 109B total / 17B active. Meta benchmarks it against Gemma 3, Gemini 2.0 Flash-Lite and Mistral 3.1, which are tier-3 classes. |
| Llama 3.2 3B / 11B Vision | `cloudflare-ai/@cf/meta/llama-3.2-3b-instruct`, `routeway/llama-3.2-3b-instruct:free`, `huggingface/meta-llama/llama-3.2-11b-instruct` | 3 | https://ai.meta.com/blog/llama-3-2-connect-2024-vision-edge-mobile-devices/ | Edge and small sizes (1B/3B text, 11B vision). |
| TheDrummer Cydonia 24B | `aihorde/aphrodite/TheDrummer/Cydonia-24B-v4.3` | 3 | https://huggingface.co/TheDrummer/Cydonia-24B-v4.3 | Roleplay finetune of Mistral-Small-3.2-24B (mistral-small is tier 3). |
| TheDrummer Skyfall 31B | `aihorde/aphrodite/TheDrummer/Skyfall-31B-v4.2` | 3 | https://huggingface.co/TheDrummer/Skyfall-31B-v4.2 | Upscaled finetune of Magistral-Small-2509 (small Mistral line, tier 3). |
| NousCoder | `ainative/nous-coder` | 3 | https://huggingface.co/NousResearch/NousCoder-14B | The only public Nous coder model is a Qwen3-14B RL finetune (qwen-14b is tier 3). The route id has no size, so tier 3 is the conservative choice. |
| Cohere Command R (08-2024) | `cohere/command-r-08-2024` | 3 | https://huggingface.co/CohereLabs/c4ai-command-r-08-2024 | About 32B, the smaller sibling of command-r-plus (tier 2). Conservative tier 3. |
| Poolside Laguna XS | `kilo-gateway/poolside/laguna-xs-2.1:free`, `routeway/laguna-xs.2:free` | 3 | https://docs.poolside.ai/release-notes/models | 33B total / 3B active. Small-active MoE, like the a3b marker. |
| Liquid LFM | `liquid/liquid-lfm-40b`, `openrouter/liquid/lfm-2.5-2.6b:free` | 3 | https://www.liquid.ai/blog/liquid-foundation-models-our-first-series-of-generative-ai-models ; https://huggingface.co/LiquidAI/LFM2.5-2.6B | LFM-40B: 40.3B total / 12B active, 32K context (2024). LFM2.5: 2.69B dense on-device model. |
| Swiss AI Apertus 70B | `publicai/swiss-ai/apertus-70b-instruct` | 3 | https://huggingface.co/swiss-ai/Apertus-70B-Instruct-2509 ; https://github.com/swiss-ai/apertus-tech-report | Llama 3.1 70B generally leads it on instruct reasoning, coding and math. That puts it below the tier-2 llama-70b reference, so conservative tier 3. |
| Reka Edge | `reka/reka-edge-2603` | 3 | https://huggingface.co/RekaAI/reka-edge-2603 | 7B total (6.4B LM + 657M vision encoder). |
| Baichuan4 (weak evidence) | `baichuan/Baichuan4` | 3 | https://www.scmp.com/tech/tech-trends/article/3263683/chinese-start-baichuan-debuts-ai-assistant-powered-upgraded-llm-eyes-ai-super-app | May 2024. Evidence is vendor and SuperCLUE (Chinese-only) claims, with no current independent placement. Conservative. |
| Baidu ERNIE 4.0 8K (weak evidence) | `baidu/ernie-4.0-8k` | 3 | https://ai.baidu.com/ai-doc/WENXINWORKSHOP/clntwmv7t | 2023 flagship with a 5K-in / 2K-out window per the Qianfan docs, and no current independent placement. Conservative. |
| ByteDance Doubao Pro 32k (weak evidence) | `doubao/doubao-pro-32k` | 3 | https://www.cluebenchmarks.com/superclue_2024 | 2024 model scored only on SuperCLUE (Chinese-only), 75.2. Conservative. |
| Tencent Hunyuan Pro (weak evidence) | `tencent/hunyuan-pro` | 3 | https://cloud.tencent.com/document/product/1729/104753 | Legacy model listed as scheduled for retirement, with no published benchmark. Conservative. |
| iFlytek Spark Max (generalv3.5, weak evidence) | `iflytek/generalv3.5` | 3 | https://www.xfyun.cn/doc/spark/Web.html | Spark 3.5 / Max API domain id. No current independent placement. Conservative. |
| StepFun Step-1V (weak evidence) | `stepfun/step-1v` | 3 | https://platform.stepfun.ai/docs/en/guides/model-migration | 2024 vision model, 100B-class; its 32K variant is already retired. Conservative. |

## Left unknown on purpose (`classify_known` stays `None`)

| Family / rows | Ids | Why no tier |
| --- | --- | --- |
| Morph v3 apply models | `morph/morph-v3-fast`, `morph/morph-v3-large` | Apply-only code-merge models (https://docs.morphllm.com/models/apply). They are not general work models, so a tier would let them into arbitrary tier-3 work. |
| Mistral Leanstral 1.5 | `requesty/mistral/leanstral-1-5` | Lean 4 proof-engineering specialist, 119B / 6B active (https://docs.mistral.ai/models/leanstral-1-5). |
| Nemotron 3.5 Content Safety | `kilo-gateway/nvidia/nemotron-3.5-content-safety:free` | 4B safety moderator built on Gemma-3-4B (https://huggingface.co/nvidia/Nemotron-3.5-Content-Safety), not a chat or work model. |
| Pollinations vendor aliases | `pollinations/*` (29 rows, e.g. `claude-large`, `openai-fast`, `qwen-safety`) | Unversioned family aliases with no distinct model behind them. Kept unknown, as BOD-419 did. |
| Routers and auto rows | `openrouter/auto`, `kilo-gateway/kilo-auto/free`, `kilo-gateway/openrouter/free`, `kilo-gateway/openrouter/auto-beta`, `bazaarlink/auto:free` | Opaque routers; the model behind them changes per request. |
| Pools and promotions | `navy/shared-pool`, `glm-cn/glm-signup-bonus` | Not model rows. |
| Stealth and vendor aliases | `opencode/big-pickle`, `opencode-zen/opencode/big-pickle`, `openrouter/stealth/ox-alpha`, `blackbox/blackboxai`, `blackbox/blackboxai-pro`, `agy/gemini-pro-agent`, `sensenova/sensechat` | No identifiable, versioned model behind the id. |

## Result on the free census (read-only, same snapshot)

| | tier 0 | tier 1 | tier 2 | tier 3 | unknown |
| --- | --- | --- | --- | --- | --- |
| origin/main d9992045 | 14 | 102 | 99 | 147 | 127 |
| BOD-427 | 14 | 127 | 131 | 170 | 47 |

## Open questions

1. Tier 0 for free models. Several families have frontier-class claims
   (MiMo-V2.5-Pro, Ling-2.6-1T, LongCat-2.0, Nemotron 3 Ultra, Muse Spark).
   They are kept at tier 1 because tier 0 doubles as the spend guard.
   Promoting any of them is a product decision.
2. The six weak-evidence legacy models (Baichuan4, ERNIE-4.0-8K,
   Doubao-pro-32k, hunyuan-pro, generalv3.5, step-1v) were put at tier 3
   rather than left unknown. Leaving them unknown is the alternative.
3. Vendor-reported benchmarks (Laguna, KAT-Coder, Hy3, MiMo) drove three of
   the tier-1 placements. A live free-model probe could confirm them later.
