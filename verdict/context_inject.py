"""Inject the compiled cheap-path context pack into the upstream request (cheap-path pack).

The admit receipt names a ``pack_digest`` (identity of the compiled artifact)
and a ``prompt_digest`` (sha256 of the compiled prompt text). This module makes
the latter describe what the upstream model actually received: the compiled
prompt is placed verbatim in one context envelope (a leading ``system`` message
for chat, a leading ``system`` input item for Responses) so anyone holding the
forwarded payload can hash the envelope and compare it with the receipt.

Rules:

* Only ``hydrated`` / ``partial`` packs whose task instructions survived
  compilation are injected. ``empty`` packs carry nothing beyond the task,
  and ``failed`` packs are never represented as executed context.
* The client's own messages are never rewritten or dropped.
* Verdict never claims injection it did not perform: the returned
  :class:`InjectionRecord` is the single source of truth for receipts/headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

INJECTABLE_PACK_STATES = frozenset({"hydrated", "partial"})
ENVELOPE_ROLE = "system"


@dataclass(frozen=True)
class InjectionRecord:
    """What was (or was not) injected, for receipts and response headers."""

    injected: bool
    pack_state: str | None
    pack_digest: str | None
    prompt_digest: str | None
    envelope_digest: str | None
    reason: str

    @property
    def digest_match(self) -> bool:
        return bool(
            self.injected
            and self.prompt_digest is not None
            and self.prompt_digest == self.envelope_digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "injected": self.injected,
            "pack_state": self.pack_state,
            "pack_digest": self.pack_digest,
            "prompt_digest": self.prompt_digest,
            "envelope_digest": self.envelope_digest,
            "digest_match": self.digest_match,
            "reason": self.reason,
        }


def envelope_digest(compiled_prompt: str) -> str:
    """Digest of the injected envelope; must equal the receipt's ``prompt_digest``."""
    return f"sha256:{sha256(compiled_prompt.encode('utf-8')).hexdigest()}"


def _skip(
    pack_state: str | None, pack_digest: str | None, prompt_digest: str | None, reason: str
) -> InjectionRecord:
    return InjectionRecord(
        injected=False,
        pack_state=pack_state,
        pack_digest=pack_digest,
        prompt_digest=prompt_digest,
        envelope_digest=None,
        reason=reason,
    )


def inject_context_pack(
    payload: dict[str, Any],
    *,
    surface: str,
    compiled_prompt: str | None,
    pack_state: str | None,
    pack_digest: str | None,
    prompt_digest: str | None,
    task_complete: bool,
) -> tuple[dict[str, Any], InjectionRecord]:
    """Return ``(forwarded_payload, record)``; ``payload`` is never mutated."""

    def skip(reason: str) -> tuple[dict[str, Any], InjectionRecord]:
        return payload, _skip(pack_state, pack_digest, prompt_digest, reason)

    if not compiled_prompt or not compiled_prompt.strip():
        return skip("no_compiled_pack")
    if not task_complete:
        return skip("task_instructions_omitted")
    if pack_state not in INJECTABLE_PACK_STATES:
        return skip(f"pack_state_{pack_state or 'unknown'}")

    digest = envelope_digest(compiled_prompt)
    if prompt_digest is not None and prompt_digest != digest:
        # The receipt would describe something other than what we are about to
        # send. Refuse rather than ship a pack the receipt cannot vouch for.
        return skip("prompt_digest_mismatch")

    envelope = {"role": ENVELOPE_ROLE, "content": compiled_prompt}
    forwarded = dict(payload)
    if surface == "responses":
        current = payload.get("input")
        if isinstance(current, list):
            forwarded["input"] = [envelope, *current]
        elif isinstance(current, str):
            forwarded["input"] = [envelope, {"role": "user", "content": current}]
        else:
            return skip("unsupported_input_shape")
    else:
        current = payload.get("messages")
        if not isinstance(current, list):
            return skip("unsupported_messages_shape")
        forwarded["messages"] = [envelope, *current]

    return forwarded, InjectionRecord(
        injected=True,
        pack_state=pack_state,
        pack_digest=pack_digest,
        prompt_digest=prompt_digest or digest,
        envelope_digest=digest,
        reason="injected_leading_system_envelope",
    )


def extract_envelope(payload: dict[str, Any], *, surface: str) -> str | None:
    """Return the leading envelope content of a forwarded payload, if present."""
    items = payload.get("input" if surface == "responses" else "messages")
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict) or first.get("role") != ENVELOPE_ROLE:
        return None
    content = first.get("content")
    return content if isinstance(content, str) else None


__all__ = [
    "ENVELOPE_ROLE",
    "INJECTABLE_PACK_STATES",
    "InjectionRecord",
    "envelope_digest",
    "extract_envelope",
    "inject_context_pack",
]
