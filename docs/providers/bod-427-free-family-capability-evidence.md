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

## Evidence source classes

Each row below labels its evidence with a source class and names who published it:

- **V (vendor-published)**: the model's creator publishes it: blog, model card, docs or technical report. Also counted as V:
  - provider/aggregator listings whose text the vendor supplies (OpenRouter, Fireworks, Cloudflare);
  - press articles that relay vendor benchmarks.
- **I (independent / arena)**: a third party measured it:
  - Artificial Analysis (AA) Intelligence Index / evaluations;
  - Vals.ai results (shown on BenchLM as "(Vals)");
  - LMSYS Chatbot Arena;
  - SuperCLUE (CLUE Chinese-language benchmark).
- **A (third-party aggregator)**: a third-party site that collects scores without running its own evals (llmbase.ai, BenchLM's non-Vals rows). Not counted as independent.

Rule (controller, 2026-10-17 13:21Z):
- When both classes exist, independent outranks vendor, and the **Decided by** column says which one set the tier.
- If applying that rule would change a tier, the tier is **not** changed. The row is flagged (⚑) and listed under "Flagged rows".

Counts over the 43 classified families:

| Source class | Families |
| --- | --- |
| V only | 33 |
| V + I | 7 |
| I only | 2 |
| V + A | 1 |

Decided by:

| Decided by | Families |
| --- | --- |
| Vendor evidence | 31 |
| Independent evidence | 2 (Nemotron 3 Super, Command A) |
| Existing classifier row | 2 (claude-4-sonnet alias, qwen3p6-plus) |
| Conservative rule | 6 |
| Spend-guard cap | 1 (Muse Spark) |
| Aggregator | 1 (Apertus) |

8 rows are flagged.

## Evidence table: classified families

| Family | Example free ids | Tier | Source class: attribution and URL(s) | Key fact used | Decided by |
| --- | --- | --- | --- | --- | --- |
| Claude 3.7 Sonnet | `api-airforce/anthropic/claude-3.7-sonnet`, `coze/claude-3-7-sonnet-20250514`, `t3-web/claude-3.7` | 1 | V: Anthropic, https://www.anthropic.com/news/claude-3-7-sonnet | Successor to Claude 3.5 Sonnet, which is already tier 1. SWE-bench Verified 62.3% (70.3% with scaffold). The coze date suffix is not the 3.7 release date; either reading is tier 1. | Vendor |
| Claude Sonnet 4 (vendor-order alias) | `deepinfra/anthropic/claude-4-sonnet` | 1 | V: Anthropic, https://www.anthropic.com/news/claude-4 | Same model as `claude-sonnet-4` (tier 1 via `sonnet-4`). SWE-bench Verified 72.7%. | Existing row (`sonnet-4`) |
| GPT-4.1 | `bluesminds/gpt-4.1`, `t3-web/gpt-4.1` | 1 | V: OpenAI, https://openai.com/index/gpt-4-1/ | SWE-bench Verified 54.6% vs 33.2% for GPT-4o, which is tier 1. `-mini`/`-nano` stay tier 3. | Vendor |
| GPT-5 (base) | `t3-web/gpt-5` | 1 | V: OpenAI, https://openai.com/index/introducing-gpt-5/ | Predates gpt-5.4 (tier 1). Tier 0 begins at gpt-5.5. | Vendor |
| GPT-5.3-Codex | `freemodel-dev/gpt-5.3-codex` | 1 | V: OpenAI, https://openai.com/index/introducing-gpt-5-3-codex/ | SWE-Bench Pro 56.8%, Terminal-Bench 2.0 77.3%. Predates gpt-5.4 (tier 1). | Vendor |
| Xiaomi MiMo-V2.5-Pro | `bazaarlink/mimo-v2.5-pro`, `deepinfra/XiaomiMiMo/MiMo-V2.5-Pro`, `huggingchat/XiaomiMiMo/MiMo-V2.5-Pro` | 1 | V: Xiaomi, https://huggingface.co/XiaomiMiMo/MiMo-V2.5-Pro ; https://mimo.mi.com/docs/en-US/updates/model | 1.02T total / 42B active. SWE-bench Verified 78.9 (FP8 card). Vendor says it matches Claude Opus 4.6 on agent work. Base MMLU-Pro 68.5 vs 69.2 for Kimi-K2 (tier 1). | Vendor |
| inclusionAI Ling-2.6-1T | `huggingchat/inclusionAI/Ling-2.6-1T`, `opencode/ling-2.6-1t-free` | 1 | V: inclusionAI / Ant Ling, https://huggingface.co/inclusionAI/Ling-2.6-1T ; https://developer.ant-ling.com/en/docs/models/ling/ | About 1T total / 63B active. Claims open-source SOTA on SWE-bench Verified, BFCL-V4 and TAU2. | Vendor |
| Meituan LongCat-2.0 | `longcat/LongCat-2.0` | 1 | V: Meituan, https://longcat.ai/blog/longcat-2.0/ ; https://github.com/meituan-longcat/longcat-2.0 | 1.6T total / ~48B active, post-trained for agentic coding, 1M context. | Vendor |
| NVIDIA Nemotron 3 Ultra | `huggingchat/nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4`, `kilo-gateway/nvidia/nemotron-3-ultra-550b-a55b:free`, `opencode-zen/opencode/nemotron-3-ultra-free` | 1 | V: NVIDIA, https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b/modelcard | 550B total / 55B active, positioned by NVIDIA for "frontier reasoning". Tier 1, not 0 (spend guard). | Vendor |
| Tencent Hy3 | `kilo-gateway/tencent/hy3:free` | 1 | V: Tencent, https://huggingface.co/tencent/Hy3 ; V (press relaying vendor numbers): VentureBeat, https://venturebeat.com/technology/tencents-apache-licensed-hy3-takes-on-glm-5-2-at-half-the-size-and-wins-everywhere-except-coding | 295B total / 21B active. SWE-bench Verified 78.0% (vendor scaffolds). Compared against GLM-5.2, which is tier 1. | Vendor |
| Kwaipilot KAT-Coder-Pro | `kilo-gateway/kwaipilot/kat-coder-pro-v2.5:free` | 1 | V: Kwaipilot tech report, https://arxiv.org/html/2607.05471 ; V (vendor-supplied listing): https://openrouter.ai/kwaipilot/kat-coder-pro-v2.5 | SWE-Bench Pro 65.2, PinchBench 94.9 (tech report). | Vendor |
| ⚑ Poolside Laguna M.1 | `kilo-gateway/poolside/laguna-m.1:free`, `routeway/laguna-m.1:free` | 1 | V: Poolside, https://docs.poolside.ai/get-started/supported-models ; A: BenchLM non-Vals rows; I: Vals.ai via BenchLM, https://benchlm.ai/compare/laguna-m-1-vs-laguna-xs-2-1 | 225B total / 23B active. Vendor-sourced numbers: SWE-bench Verified 74.6%, SWE-bench Pro 49.2%. Independent Vals numbers are lower: SWE-bench 57.6%, GPQA Diamond 27.0%, MMLU-Pro 68.8%, Terminal-Bench 2.1 34.1%. | Vendor (independent points lower, see flag) |
| Poolside Laguna S 2.1 | `nara/laguna-s-2.1` | 1 | V: Poolside, https://poolside.ai/blog/introducing-laguna-s-2-1 ; https://docs.poolside.ai/get-started/supported-models | 118B total / 8B active. SWE-bench Pro 59.4%, SWE-bench Multilingual 78.5%. | Vendor |
| ⚑ Meta Muse Spark | `muse-spark-web/muse-spark`, `-thinking`, `-contemplating` | 1 | V: Meta, https://ai.meta.com/blog/introducing-muse-spark-msl/ ; I: Artificial Analysis, https://artificialanalysis.ai/articles/muse-spark-1-3 | AA Intelligence Index: Muse Spark 1.3 (xhigh) 61, tied with GPT-5.6 Sol (max); 1.3 (max) 62. Meta notes gaps in long-horizon agentic work and coding. The web route is unversioned. | Independent supports at least tier 1; tier 0 held back by the spend guard and the 13:21Z ruling |
| ⚑ AI21 Jamba Large 1.7 | `ai21/jamba-large-1.7` | 2 | V: AI21, https://huggingface.co/ai21labs/AI21-Jamba-Large-1.7 ; I: Artificial Analysis, https://artificialanalysis.ai/models/jamba-1-7-large | 398B total / 94B active. AA Intelligence Index 6 (estimated), "among the least intelligent" open non-reasoning models of its size (class median 12). | Vendor (size class); independent points lower, see flag |
| ⚑ Llama 4 Maverick | `ainative/llama-4-maverick`, `bazaarlink/llama-4-maverick`, `t3-web/llama-4-maverick` | 2 | V: Meta, https://ai.meta.com/blog/llama-4-multimodal-intelligence/ ; I: Artificial Analysis, https://artificialanalysis.ai/models/comparisons/llama-4-maverick-vs-gpt-4o | 400B total / 17B active. AA Intelligence Index: Maverick 9 vs GPT-4o (Nov '24) 8, i.e. at or above GPT-4o, which is tier 1. This corrects the earlier unsourced note that independent results were lower. Ids with an explicit `17b` stay tier 3 (main). | Vendor-era conservative placement; independent points higher, see flag |
| Arcee Trinity Large | `arcee-ai/arcee-ai/trinity-large-preview:free`, `opencode/trinity-large-preview-free` | 2 | V: Arcee, https://huggingface.co/arcee-ai/Trinity-Large-Preview | 398B total / 13B active, "lightly post-trained". Vendor table: MMLU-Pro 75.2 vs 80.5 for Maverick, GPQA 63.3 vs 69.8. | Vendor (anchored to the Maverick row) |
| AionLabs Aion 2.x/3.0 | `aion/aion-labs/aion-2.0`, `aion-2.5`, `aion-3.0` | 2 | V (vendor-supplied listings): https://openrouter.ai/aion-labs/aion-2.0 ; https://openrouter.ai/aion-labs/aion-3.0 ; https://llmtr.com/models/aion-labs/aion-2.5 | 2.0 and 2.5 are DeepSeek V3.2-based roleplay models (deepseek-3.2 is tier 2). 3.0 is a GLM-based roleplay system. `-mini` ids stay tier 3. | Vendor |
| ⚑ xAI Grok 2 | `api-airforce/x-ai/grok-2-1212` | 2 | V: xAI, https://x.ai/news/grok-2 ; I: LMSYS Chatbot Arena, as relayed in that xAI post | Arena: the early Grok-2 build ranked above Claude 3.5 Sonnet and GPT-4 Turbo, third overall behind GPT-4o. Claude 3.5 Sonnet and GPT-4o are both tier 1. Placed at tier 2 as the grok-3 predecessor. | Generation rule (vendor lineage); independent points higher, see flag |
| Xiaomi MiMo-V2.5 | `bazaarlink/mimo-v2.5`, `deepinfra/XiaomiMiMo/MiMo-V2.5`, `opencode-zen/opencode/mimo-v2.5-free` | 2 | V: Xiaomi, https://huggingface.co/XiaomiMiMo/MiMo-V2.5-Pro | 310B total / 15B active. Base MMLU-Pro 65.8 vs 68.5 for Pro, GPQA 58.1 vs 66.7. | Vendor |
| NVIDIA Nemotron 3 Super | `nvidia/nvidia/nemotron-3-super-120b-a12b`, `ollama-cloud/nemotron-3-super`, `opencode/nemotron-3-super-free` (+4) | 2 | V: NVIDIA, https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/modelcard ; I: Artificial Analysis, https://artificialanalysis.ai/models/comparisons/nvidia-nemotron-3-super-120b-a12b-vs-gpt-oss-120b | AA Intelligence Index 13 vs 12 for gpt-oss-120b (tier 2), so the same class. | Independent |
| Cohere Command A (+reasoning, vision) | `cohere/command-a-03-2025`, `cohere/command-a-reasoning-08-2025`, `huggingchat/CohereLabs/command-a-vision-07-2025` | 2 | V: Cohere, https://cohere.com/blog/command-a ; I: Artificial Analysis, https://artificialanalysis.ai/models/comparisons/command-a-vs-gpt-4o | Vendor claims GPT-4o / DeepSeek-V3 parity. AA rates both GPT-4o and DeepSeek-V3 above Command A. Placed in the deepseek-v3 class. | Independent |
| Nous Hermes 3/4 (70B, 405B) | `deepinfra/NousResearch/Hermes-3-Llama-3.1-405B`, `nous-research/Hermes-4-405B`, `nous-research/Hermes-4-70B` | 2 | V: Nous Research, https://arxiv.org/abs/2508.18255 ; https://huggingface.co/NousResearch/Hermes-3-Llama-3.1-405B | Llama-3.1 finetunes. Hermes 4 405B (reasoning mode): GPQA 70.6, LiveCodeBench 61.4. `Hermes-3-Llama-3.1-70B` is already tier 2. | Vendor |
| Qwen3.6 Plus (Fireworks spelling) | `fireworks/qwen3p6-plus` | 2 | V (provider listing): Fireworks, https://fireworks.ai/models/fireworks/qwen3p6-plus | Same model as `qwen3.6-plus`, which is tier 2 via the BOD-419 qwen-plus row. | Existing row (qwen-plus) |
| Qwen-SEA-LION v4 32B | `publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT`, `sealion/aisingapore/Qwen-SEA-LION-v4-32B-IT` | 2 | V: AI Singapore, https://huggingface.co/aisingapore/Qwen-SEA-LION-v4-32B-IT | Built on Qwen3-32B (qwen-32b is tier 2). | Vendor |
| Ai2 Olmo 3 32B Think | `publicai/allenai/Olmo-3-32B-Think` | 2 | V: Ai2, https://huggingface.co/allenai/Olmo-3-32B-Think ; https://allenai.org/blog/olmo3 | Roughly Qwen3-32B level: ahead on MATH, AIME25 and IFEval, behind on LiveCodeBench, GPQA and MMLU. | Vendor |
| GPT-5.6 Luna | `duckduckgo-web/gpt-5.6-luna` | 3 | V: OpenAI, https://developers.openai.com/api/docs/models/gpt-5.6-luna ; V (provider listing): Cloudflare, https://developers.cloudflare.com/ai/models/openai/gpt-5.6-luna/ | OpenAI's "cost-sensitive, high-volume" tier at $0.20/$1.20 per M tokens. Main already excludes luna from tier 0. | Vendor |
| Llama 4 Scout | `bazaarlink/llama-4-scout`, `t3-web/llama-4-scout` | 3 | V: Meta, https://ai.meta.com/blog/llama-4-multimodal-intelligence/ | 109B total / 17B active. Meta benchmarks it against Gemma 3, Gemini 2.0 Flash-Lite and Mistral 3.1, which are tier-3 classes. | Vendor |
| Llama 3.2 3B / 11B Vision | `cloudflare-ai/@cf/meta/llama-3.2-3b-instruct`, `routeway/llama-3.2-3b-instruct:free`, `huggingface/meta-llama/llama-3.2-11b-instruct` | 3 | V: Meta, https://ai.meta.com/blog/llama-3-2-connect-2024-vision-edge-mobile-devices/ | Edge and small sizes. | Vendor |
| TheDrummer Cydonia 24B | `aihorde/aphrodite/TheDrummer/Cydonia-24B-v4.3` | 3 | V: TheDrummer (finetuner), https://huggingface.co/TheDrummer/Cydonia-24B-v4.3 | Finetune of Mistral-Small-3.2-24B (mistral-small is tier 3). | Vendor |
| TheDrummer Skyfall 31B | `aihorde/aphrodite/TheDrummer/Skyfall-31B-v4.2` | 3 | V: TheDrummer (finetuner), https://huggingface.co/TheDrummer/Skyfall-31B-v4.2 | Upscaled Magistral-Small-2509 finetune. | Vendor |
| NousCoder | `ainative/nous-coder` | 3 | V: Nous Research, https://huggingface.co/NousResearch/NousCoder-14B | The only public Nous coder is a Qwen3-14B finetune (qwen-14b is tier 3). The route id has no size. | Vendor |
| Cohere Command R (08-2024) | `cohere/command-r-08-2024` | 3 | V: Cohere, https://huggingface.co/CohereLabs/c4ai-command-r-08-2024 | About 32B, the smaller sibling of command-r-plus (tier 2). | Vendor (conservative) |
| Poolside Laguna XS | `kilo-gateway/poolside/laguna-xs-2.1:free`, `routeway/laguna-xs.2:free` | 3 | V: Poolside, https://docs.poolside.ai/release-notes/models | 33B total / 3B active, a small-active MoE. | Vendor |
| Liquid LFM | `liquid/liquid-lfm-40b`, `openrouter/liquid/lfm-2.5-2.6b:free` | 3 | V: Liquid AI, https://www.liquid.ai/blog/liquid-foundation-models-our-first-series-of-generative-ai-models ; https://huggingface.co/LiquidAI/LFM2.5-2.6B | LFM-40B: 40.3B total / 12B active, 32K context. LFM2.5: 2.69B dense. | Vendor |
| ⚑ Swiss AI Apertus 70B | `publicai/swiss-ai/apertus-70b-instruct` | 3 | V: Swiss AI, https://huggingface.co/swiss-ai/Apertus-70B-Instruct-2509 ; https://github.com/swiss-ai/apertus-tech-report ; A: llmbase.ai, https://llmbase.ai/compare/apertus-70b-instruct,llama-3-1-instruct-70b/ | The vendor card shows only pretraining parity with Llama-3.1-70B (avg 67.5 vs 67.3). The "Llama 3.1 70B leads on instruct reasoning/coding/math" comparison comes from the aggregator, not the vendor. | Aggregator (conservative), see flag |
| Reka Edge | `reka/reka-edge-2603` | 3 | V: Reka, https://huggingface.co/RekaAI/reka-edge-2603 | 7B total. | Vendor |
| ⚑ Baichuan4 (weak evidence) | `baichuan/Baichuan4` | 3 | I: SuperCLUE (Chinese-only), as reported by SCMP, https://www.scmp.com/tech/tech-trends/article/3263683/chinese-start-baichuan-debuts-ai-assistant-powered-upgraded-llm-eyes-ai-super-app | May 2024. SuperCLUE 80.64, ranked first and above GPT-4-Turbo-0125. Chinese-only benchmark with no current placement. | Conservative rule (13:21Z ruling keeps tier 3), see flag |
| Baidu ERNIE 4.0 8K (weak evidence) | `baidu/ernie-4.0-8k` | 3 | V: Baidu Qianfan docs, https://ai.baidu.com/ai-doc/WENXINWORKSHOP/clntwmv7t | 2023 flagship, 5K-in / 2K-out window. No independent placement cited. | Conservative rule |
| ⚑ ByteDance Doubao Pro 32k (weak evidence) | `doubao/doubao-pro-32k` | 3 | I: SuperCLUE 2024 annual report (Chinese-only), https://www.cluebenchmarks.com/superclue_2024 | SuperCLUE 75.2 overall (Dec 2024 build). Not mapped to a classifier anchor. | Conservative rule (13:21Z ruling keeps tier 3), see flag |
| Tencent Hunyuan Pro (weak evidence) | `tencent/hunyuan-pro` | 3 | V: Tencent Cloud docs, https://cloud.tencent.com/document/product/1729/104753 | Legacy model, listed as scheduled for retirement. | Conservative rule |
| iFlytek Spark Max (generalv3.5, weak evidence) | `iflytek/generalv3.5` | 3 | V: iFlytek docs, https://www.xfyun.cn/doc/spark/Web.html | Spark 3.5 / Max API domain id. | Conservative rule |
| StepFun Step-1V (weak evidence) | `stepfun/step-1v` | 3 | V: StepFun docs, https://platform.stepfun.ai/docs/en/guides/model-migration | 2024 vision model, 100B-class; its 32K variant is already retired. | Conservative rule |

## Flagged rows (rule would change the tier; tier NOT changed)

| Row | Current tier | Tier the independent-first rule points to | Why |
| --- | --- | --- | --- |
| Poolside Laguna M.1 | 1 | 2 or lower | Vals: SWE-bench 57.6%, GPQA Diamond 27.0%, MMLU-Pro 68.8%, well below the vendor-sourced 74.6% SWE-bench Verified that set tier 1. |
| Meta Muse Spark | 1 | 0 | AA index 61–62, tied with GPT-5.6 Sol (max), a tier-0 anchor. Already covered by the 13:21Z ruling (frontier-claim families stay tier 1). |
| AI21 Jamba Large 1.7 | 2 | 3 | AA index 6 (estimated), bottom of its size class (median 12). |
| Llama 4 Maverick | 2 | 1 | AA index 9 vs GPT-4o 8 (tier 1). Trinity Large is anchored to this row, so it would follow. |
| xAI Grok 2 | 2 | 1 | LMSYS Arena placed it above Claude 3.5 Sonnet, behind only GPT-4o and Gemini 1.5 at the time (both tier-1 anchors). |
| Swiss AI Apertus 70B | 3 | 2 | The only instruct-level comparison is from an aggregator (not independent). The vendor card alone shows Llama-3.1-70B parity (tier 2). |
| Baichuan4 | 3 | 2 or higher | SuperCLUE (independent, Chinese-only) ranked it above GPT-4-Turbo. Already covered by the 13:21Z ruling (legacy six stay tier 3). |
| ByteDance Doubao Pro 32k | 3 | possibly 2 | SuperCLUE 75.2. No classifier anchor scored in the same report was checked. Covered by the 13:21Z ruling. |

Cross-cutting note: AA rates gpt-oss-120b (tier-2 anchor, 12) above GPT-4o (tier-1 anchor, 7–9). So AA-based placement depends on which anchor you compare against. That is why the Maverick and Nemotron 3 Super rows can point in different directions.

## Left unknown on purpose (`classify_known` stays `None`)

| Family / rows | Ids | Why no tier | Source class |
| --- | --- | --- | --- |
| Morph v3 apply models | `morph/morph-v3-fast`, `morph/morph-v3-large` | Apply-only code-merge models (https://docs.morphllm.com/models/apply). They are not general work models, so a tier would let them into arbitrary tier-3 work. | V: Morph docs |
| Mistral Leanstral 1.5 | `requesty/mistral/leanstral-1-5` | Lean 4 proof-engineering specialist, 119B / 6B active (https://docs.mistral.ai/models/leanstral-1-5). | V: Mistral docs |
| Nemotron 3.5 Content Safety | `kilo-gateway/nvidia/nemotron-3.5-content-safety:free` | 4B safety moderator built on Gemma-3-4B (https://huggingface.co/nvidia/Nemotron-3.5-Content-Safety), not a chat or work model. | V: NVIDIA model card |
| Pollinations vendor aliases | `pollinations/*` (29 rows, e.g. `claude-large`, `openai-fast`, `qwen-safety`) | Unversioned family aliases with no distinct model behind them. Kept unknown, as BOD-419 did. | n/a (no identifiable model) |
| Routers and auto rows | `openrouter/auto`, `kilo-gateway/kilo-auto/free`, `kilo-gateway/openrouter/free`, `kilo-gateway/openrouter/auto-beta`, `bazaarlink/auto:free` | Opaque routers; the model behind them changes per request. | n/a (no identifiable model) |
| Pools and promotions | `navy/shared-pool`, `glm-cn/glm-signup-bonus` | Not model rows. | n/a (not model rows) |
| Stealth and vendor aliases | `opencode/big-pickle`, `opencode-zen/opencode/big-pickle`, `openrouter/stealth/ox-alpha`, `blackbox/blackboxai`, `blackbox/blackboxai-pro`, `agy/gemini-pro-agent`, `sensenova/sensechat` | No identifiable, versioned model behind the id. | n/a (no identifiable model) |

## Result on the free census (read-only, same snapshot)

| | tier 0 | tier 1 | tier 2 | tier 3 | unknown |
| --- | --- | --- | --- | --- | --- |
| origin/main d9992045 | 14 | 102 | 99 | 147 | 127 |
| BOD-427 | 14 | 127 | 131 | 170 | 47 |

## Controller rulings (2026-10-17 13:21Z) and open questions

Rulings:
- Tiers stay as they are.
- The five frontier-claim families stay at tier 1.
- The six weak-evidence legacy models stay at tier 3.
- The 781 FREE-capacity routes outside the census stay out of scope.

Open questions: the flagged rows above (Laguna M.1, Jamba Large 1.7, Llama 4 Maverick (Trinity Large follows it), Grok 2, Apertus 70B). Muse Spark, Baichuan4 and Doubao are flagged too but already covered by the rulings. Tiers are unchanged pending a decision.
