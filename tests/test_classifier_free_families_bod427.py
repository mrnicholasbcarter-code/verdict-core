"""BOD-427: evidence-backed tiers for the leftover free-model families.

Two guards live here:

* ``test_main_classifications_never_re_tier`` loads a snapshot of every id
  that ``classify_known`` already classified on origin/main (d9992045): the
  live free-tier census (489 ids), the full catalog cache and the gated
  census catalog (10,050 ids, 7,235 known). Each must keep its exact tier.
* ``test_leftover_free_id_tier`` pins every free-census id that was still
  unknown on main to the tier recorded in
  ``docs/providers/bod-427-free-family-capability-evidence.md`` (``None``
  means deliberately left unknown: aliases, routers and special-purpose
  models).

Tier semantics come from ``verdict/classifier.py`` CAPABILITY_PATTERNS:
0 = frontier spend guard, 1 = strong near-frontier (sonnet-4 / gpt-4o /
deepseek-r1 / qwen-235b class), 2 = mid (deepseek-v3 / llama-70b /
qwen-72b / gpt-oss-120b class), 3 = cheapest/fastest (small sizes, fast
serving tiers, small-active MoE).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.classifier import classify, classify_known

_FIXTURES = Path(__file__).parent / "fixtures" / "catalog_truth"
_MAIN_SNAPSHOT = _FIXTURES / "classifier_main_tiers_bod427.json"


def _main_snapshot() -> dict[str, int]:
    payload = json.loads(_MAIN_SNAPSHOT.read_text(encoding="utf-8"))
    return {model_id: int(tier) for tier, ids in payload["tiers"].items() for model_id in ids}


def test_main_snapshot_is_complete() -> None:
    snapshot = _main_snapshot()
    payload = json.loads(_MAIN_SNAPSHOT.read_text(encoding="utf-8"))
    assert len(snapshot) == payload["known_count"] == 7235
    assert set(payload["tiers"]) == {"0", "1", "2", "3"}


def test_main_classifications_never_re_tier() -> None:
    """ZERO re-tiers: every id classified on main keeps its exact tier."""
    re_tiered = {
        model_id: (tier, classify_known(model_id))
        for model_id, tier in _main_snapshot().items()
        if classify_known(model_id) != tier or classify(model_id) != tier
    }
    assert re_tiered == {}


_POLLINATIONS_ALIASES = [
    "claude",
    "claude-fast",
    "claude-large",
    "deepseek",
    "gemini",
    "gemini-fast",
    "gemini-large",
    "gemini-search",
    "glm",
    "grok",
    "grok-large",
    "kimi",
    "midijourney",
    "midijourney-large",
    "minimax",
    "mistral",
    "nova",
    "nova-fast",
    "openai",
    "openai-fast",
    "openai-large",
    "perplexity-fast",
    "perplexity-reasoning",
    "polly",
    "qwen-coder",
    "qwen-coder-large",
    "qwen-large",
    "qwen-safety",
    "qwen-vision",
]

# Every id that classify_known returned None for on main, from the live
# free-tier census (admit-last-good perModel, fetched 2026-10-17T12:14:47Z,
# 489 ids, 127 unknown). Tier per the BOD-427 evidence doc.
LEFTOVER_FREE_IDS: dict[str, int | None] = {
    # --- tier 1: strong near-frontier (evidence: SWE-bench / vendor cards)
    "api-airforce/anthropic/claude-3.7-sonnet": 1,
    "coze/claude-3-7-sonnet-20250514": 1,
    "t3-web/claude-3.7": 1,
    "deepinfra/anthropic/claude-4-sonnet": 1,
    "bluesminds/gpt-4.1": 1,
    "t3-web/gpt-4.1": 1,
    "t3-web/gpt-5": 1,
    "freemodel-dev/gpt-5.3-codex": 1,
    "bazaarlink/mimo-v2.5-pro": 1,
    "deepinfra/XiaomiMiMo/MiMo-V2.5-Pro": 1,
    "huggingchat/XiaomiMiMo/MiMo-V2.5-Pro": 1,
    "huggingchat/inclusionAI/Ling-2.6-1T": 1,
    "opencode/ling-2.6-1t-free": 1,
    "longcat/LongCat-2.0": 1,
    "huggingchat/nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4": 1,
    "kilo-gateway/nvidia/nemotron-3-ultra-550b-a55b:free": 1,
    "opencode-zen/opencode/nemotron-3-ultra-free": 1,
    "kilo-gateway/tencent/hy3:free": 1,
    "kilo-gateway/kwaipilot/kat-coder-pro-v2.5:free": 1,
    "kilo-gateway/poolside/laguna-m.1:free": 1,
    "routeway/laguna-m.1:free": 1,
    "nara/laguna-s-2.1": 1,
    "muse-spark-web/muse-spark": 1,
    "muse-spark-web/muse-spark-thinking": 1,
    "muse-spark-web/muse-spark-contemplating": 1,
    # --- tier 2: mid (deepseek-v3 / llama-70b / gpt-oss-120b class)
    "ai21/jamba-large-1.7": 2,
    "ainative/llama-4-maverick": 2,
    "bazaarlink/llama-4-maverick": 2,
    "t3-web/llama-4-maverick": 2,
    "aion/aion-labs/aion-2.0": 2,
    "aion/aion-labs/aion-2.5": 2,
    "aion/aion-labs/aion-3.0": 2,
    "api-airforce/x-ai/grok-2-1212": 2,
    "arcee-ai/arcee-ai/trinity-large-preview:free": 2,
    "opencode/trinity-large-preview-free": 2,
    "bazaarlink/mimo-v2.5": 2,
    "deepinfra/XiaomiMiMo/MiMo-V2.5": 2,
    "opencode-zen/opencode/mimo-v2.5-free": 2,
    "bazaarlink/nemotron-3-super-120b-a12b": 2,
    "deepinfra/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B": 2,
    "kilo-gateway/nvidia/nemotron-3-super-120b-a12b:free": 2,
    "nvidia/nvidia/nemotron-3-super-120b-a12b": 2,
    "ollama-cloud/nemotron-3-super": 2,
    "opencode-zen/opencode/nemotron-3-super-free": 2,
    "opencode/nemotron-3-super-free": 2,
    "cohere/command-a-03-2025": 2,
    "cohere/command-a-reasoning-08-2025": 2,
    "cohere/command-a-vision-07-2025": 2,
    "huggingchat/CohereLabs/command-a-reasoning-08-2025": 2,
    "huggingchat/CohereLabs/command-a-vision-07-2025": 2,
    "deepinfra/NousResearch/Hermes-3-Llama-3.1-405B": 2,
    "nous-research/Hermes-4-405B": 2,
    "nous-research/Hermes-4-70B": 2,
    "fireworks/qwen3p6-plus": 2,
    "publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT": 2,
    "sealion/aisingapore/Qwen-SEA-LION-v4-32B-IT": 2,
    "publicai/allenai/Olmo-3-32B-Think": 2,
    # --- tier 3: cheapest / small / weak-evidence legacy (conservative)
    "aihorde/aphrodite/TheDrummer/Cydonia-24B-v4.3": 3,
    "aihorde/aphrodite/TheDrummer/Skyfall-31B-v4.2": 3,
    "ainative/nous-coder": 3,
    "bazaarlink/llama-4-scout": 3,
    "t3-web/llama-4-scout": 3,
    "cloudflare-ai/@cf/meta/llama-3.2-3b-instruct": 3,
    "hyperbolic/meta-llama/Llama-3.2-3B-Instruct": 3,
    "routeway/llama-3.2-3b-instruct:free": 3,
    "huggingface/meta-llama/llama-3.2-11b-instruct": 3,
    "cohere/command-r-08-2024": 3,
    "duckduckgo-web/gpt-5.6-luna": 3,
    "kilo-gateway/poolside/laguna-xs-2.1:free": 3,
    "routeway/laguna-xs.2:free": 3,
    "liquid/liquid-lfm-40b": 3,
    "openrouter/liquid/lfm-2.5-2.6b:free": 3,
    "publicai/swiss-ai/apertus-70b-instruct": 3,
    "reka/reka-edge-2603": 3,
    "baichuan/Baichuan4": 3,
    "baidu/ernie-4.0-8k": 3,
    "doubao/doubao-pro-32k": 3,
    "tencent/hunyuan-pro": 3,
    "iflytek/generalv3.5": 3,
    "stepfun/step-1v": 3,
    # --- left unknown: special-purpose models (not general work models)
    "morph/morph-v3-fast": None,
    "morph/morph-v3-large": None,
    "requesty/mistral/leanstral-1-5": None,
    "kilo-gateway/nvidia/nemotron-3.5-content-safety:free": None,
    # --- left unknown: routers, pools, stealth and vendor-alias rows
    "agy/gemini-pro-agent": None,
    "bazaarlink/auto:free": None,
    "blackbox/blackboxai": None,
    "blackbox/blackboxai-pro": None,
    "glm-cn/glm-signup-bonus": None,
    "kilo-gateway/kilo-auto/free": None,
    "kilo-gateway/openrouter/auto-beta": None,
    "kilo-gateway/openrouter/free": None,
    "navy/shared-pool": None,
    "opencode-zen/opencode/big-pickle": None,
    "opencode/big-pickle": None,
    "openrouter/auto": None,
    "openrouter/stealth/ox-alpha": None,
    "sensenova/sensechat": None,
    **{f"pollinations/{alias}": None for alias in _POLLINATIONS_ALIASES},
}


def test_leftover_table_covers_the_whole_census_remainder() -> None:
    assert len(LEFTOVER_FREE_IDS) == 127
    assert sum(tier is None for tier in LEFTOVER_FREE_IDS.values()) == 47
    assert not set(LEFTOVER_FREE_IDS) & set(_main_snapshot())


@pytest.mark.parametrize(("route_id", "tier"), sorted(LEFTOVER_FREE_IDS.items()))
def test_leftover_free_id_tier(route_id: str, tier: int | None) -> None:
    assert classify_known(route_id) == tier


@pytest.mark.parametrize(
    "route_id",
    [
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "openai/gpt-5-mini",
        "meta/llama-4-scout-17b-16e-instruct",
        "x-ai/grok-2-mini",
        "xiaomi/mimo-v2.5-flash",
        "aion-labs/aion-3.5-mini",
    ],
)
def test_cheap_variants_of_new_families_stay_cheap(route_id: str) -> None:
    assert classify_known(route_id) == 3


def test_new_strong_families_never_reach_the_frontier_guard() -> None:
    """Tier 0 stays the frontier spend guard (eligibility _task_gate)."""
    new_tiers = {tier for tier in LEFTOVER_FREE_IDS.values() if tier is not None}
    assert 0 not in new_tiers
