/*
 * Frozen excerpt of sources.js from vava-nessa/free-coding-models:
 * https://github.com/vava-nessa/free-coding-models/blob/ffb2d3ec49d7f9445d35cad2bc4640b5731c1b12/sources.js
 * Only selected provider declarations and their model rows are included.
 * Do not execute: this fixture is parser input only.
 *
 * MIT License with Enhanced Disclaimers
 * Copyright (c) 2025-2026 vava-nessa
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 *
 * Upstream enhanced disclaimers:
 * https://github.com/vava-nessa/free-coding-models/blob/ffb2d3ec49d7f9445d35cad2bc4640b5731c1b12/LICENSE
 */

// 📖 NIM source - https://build.nvidia.com
export const nvidiaNim = [
  // Audit (2026-09-09): GET /v1/models lists 80 ids but most are ghost catalog entries that
  // answer 404 "Function not found" to a 1-token chat completion on the free integrate API.
  ['z-ai/glm-5.3-flash', 'GLM-5.3-Flash', 'S+', '-', '1M'], // Added (2026-09-22) — live on NIM /v1/models (needed a 230s cold start on first probe)
  ['nvidia/nemotron-3.5-super-vl-120b-a12b', 'Nemotron 3.5 Super VL', 'S+', '71.07%', '262k'], // Added (2026-10-17 audit) — new hosted model, live on /v1/models + build.nvidia.com; multimodal 120B/12B MoE, SWE-bench Verified 71.07% per the NVIDIA model card
];

export const cerebras = [
  // Removed (2026-08-23): zai-glm-4.7 (GLM 4.7) — shutdown 2026-08-17 per Cerebras official notice
  // ── S tier — SWE-bench Verified 60–70% ──
  ['gpt-oss-120b', 'GPT OSS 120B', 'S', '62.4%', '65k'], // Fixed (2026-07-27): ctx '128k' → '65k' (free tier per official docs)
  ['qwen-3.8-27b', 'Qwen 3.8 27B', 'A+', '-', '64k'],
];

export const cheapllm = [
  // ── S+ tier — SWE-bench Verified ≥70% ──
  // 📖 No published SWE-bench Verified score, so '-'; tier follows the DeepSeek V4.1 Flash
  // 📖 rows already listed for Ollama Cloud and Runcrate (same model family).
  ['deepseek-v4.1-flash-1017', 'DeepSeek V4.1 Flash 1017', 'S+', '-', '262k'],
];

export const sources = {
  nvidia: {
    name: 'NVIDIA NIM',
    url: 'https://integrate.api.nvidia.com/v1/chat/completions',
    quota: 'Free · 1000 req/month',
    quotaCode: 'free',
    models: nvidiaNim,
  },
  cerebras: {
    name: 'Cerebras',
    url: 'https://api.cerebras.ai/v1/chat/completions',
    quota: 'Free trial · $5 credits / 30 days · card required',
    quotaCode: 'limited',
    models: cerebras,
  },
  cheapllm: {
    name: 'cheapllm.shop',
    url: 'https://cheapllm.shop/v1/chat/completions',
    quota: 'Free · no rate limit during the beta · then 5k in / 1k out tokens/hour',
    quotaCode: 'free',
    models: cheapllm,
  },
};

// 📖 Flatten all models from all sources — each entry includes providerKey as 6th element
// 📖 providerKey lets the main CLI know which API key and URL to use per model
// 📖 Models with a deprecatedAfter date (7th tuple element) are auto-filtered after that date
export const MODELS = [];
const _today = new Date().toISOString().split('T')[0];
for (const [sourceKey, sourceData] of Object.entries(sources)) {
  if (!sourceData || !sourceData.models) continue;
  for (const model of sourceData.models) {
    const [modelId, label, tier, sweScore, ctx, addedDate, deprecatedAfter] = model;
    if (deprecatedAfter && _today > deprecatedAfter) continue;
    MODELS.push([modelId, label, tier, sweScore, ctx, sourceKey, addedDate || null]);
  }
}
