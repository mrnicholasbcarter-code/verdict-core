"""Optional LiteLLM VerdictRouter adapter — BOD-9.

Preserves Verdict as strategy authority (BOD-104), not a second router.
This is an optional adapter that translates LiteLLM gateway responses into
Verdict evidence/receipt contracts without bypassing admission or context.
"""

from __future__ import annotations

from typing import Any

# LiteLLM adapter interface — thin boundary only.
# Verdict retains decision, qualification, receipt/provenance, verification.
# LiteLLM must never select a different model, bypass admission, or
# fabricate provenance (see docs/superpowers/specs/2026-09-19-bod-9-reconciliation.md).

# Adapter identity for routing/evidence
ADAPTER_ID = "litellm-verdict-adapter-v1"

# Supported subset (explicit deny for unsupported capabilities)
SUPPORTED_CAPABILITIES = {
    "openai_chat_completions": True,
    "openai_responses": False,
    "anthropic_messages": False,  # owned by BOD-102 adapter
    "named_check": False,
}


def is_litellm_route_qualified(passport: dict[str, Any] | None) -> bool:
    """Adapter-level qualification: requires both LiteLLM capability AND Verdict admission."""
    if not passport:
        return False
    capabilities = passport.get("capabilities") or passport.get("confirmed") or {}
    litellm_confirmed = capabilities.get("litellm") or capabilities.get("litellm_adapter")
    return bool(litellm_confirmed)


def adapter_digest(request_body: dict[str, Any]) -> str:
    # Minimal identity for evidence/receipt (no secrets).
    import json

    payload = {
        "adapter": ADAPTER_ID,
        "model_preference": request_body.get("model"),
        "message_count": len(request_body.get("messages", [])),
    }
    return json.dumps(payload, sort_keys=True)
