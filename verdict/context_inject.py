"""Inject the compiled cheap-path context pack into the upstream request.

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

BOD-333 phase 1 (opt-in, product-owned seam only):

* :class:`ContextOutputPolicy` is OFF by default. When ``enabled`` is
  ``False`` (the default), :func:`inject_context_pack` behaviour and
  :meth:`InjectionRecord.to_dict` output are byte-identical to the
  pre-BOD-333 shape -- no new keys, no altered envelope bytes.
* When a caller opts in, this phase-1 seam does not run a new compression
  engine (none exists yet; see PRODUCT-SCOPE phase 2). It only attaches a
  raw-artifact pointer (digest + byte count of the exact envelope that was
  sent) and records whether any ``cache_control`` mark on the envelope
  content survived verbatim. A lossy/rejected transform is not implemented
  here, so the observed behaviour is a raw fallback by construction.
* Verdict's Prime adapter cannot intercept tool calls/results inside a
  Prime-owned worker loop (``verdict/harness_prime.py`` certify():
  ``tool_interception`` / ``tool_pre_post`` are both ``"unsupported"``).
  This seam only ever governs the envelope Verdict itself injects, never a
  Prime-owned tool loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

INJECTABLE_PACK_STATES = frozenset({"hydrated", "partial"})
ENVELOPE_ROLE = "system"


@dataclass(frozen=True)
class ContextOutputPolicy:
    """Opt-in context-output policy. Default ``enabled=False`` is a no-op.

    Phase 1 carries no compression engine (PRODUCT-SCOPE phase 2 gap G1).
    ``enabled=True`` only attaches observability fields (raw-artifact
    pointer, cache_control preservation flag); it never mutates the
    envelope bytes that are forwarded upstream.
    """

    enabled: bool = False
    estimate_method: str = "raw_passthrough"


@dataclass(frozen=True)
class RawArtifactPointer:
    """Digest + byte count of the exact raw envelope, retrievable by digest."""

    digest: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "bytes": self.bytes}


@dataclass(frozen=True)
class InjectionRecord:
    """What was (or was not) injected, for receipts and response headers."""

    injected: bool
    pack_state: str | None
    pack_digest: str | None
    prompt_digest: str | None
    envelope_digest: str | None
    reason: str
    # BOD-333 phase 1 (opt-in): None unless a caller passes a ContextOutputPolicy
    # with enabled=True. None keeps to_dict() byte-identical to pre-BOD-333 output.
    raw_artifact_pointer: RawArtifactPointer | None = field(default=None)
    cache_control_preserved: bool | None = field(default=None)

    @property
    def digest_match(self) -> bool:
        return bool(
            self.injected
            and self.prompt_digest is not None
            and self.prompt_digest == self.envelope_digest
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "injected": self.injected,
            "pack_state": self.pack_state,
            "pack_digest": self.pack_digest,
            "prompt_digest": self.prompt_digest,
            "envelope_digest": self.envelope_digest,
            "digest_match": self.digest_match,
            "reason": self.reason,
        }
        # Omitted entirely (not even as null) when the opt-in policy never ran,
        # so default-off receipts/headers are byte-identical to pre-BOD-333.
        if self.raw_artifact_pointer is not None or self.cache_control_preserved is not None:
            out["raw_artifact_pointer"] = (
                self.raw_artifact_pointer.to_dict() if self.raw_artifact_pointer else None
            )
            out["cache_control_preserved"] = self.cache_control_preserved
        return out


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
    policy: ContextOutputPolicy | None = None,
) -> tuple[dict[str, Any], InjectionRecord]:
    """Return ``(forwarded_payload, record)``; ``payload`` is never mutated.

    ``policy`` is opt-in and defaults to ``None`` (equivalent to
    ``ContextOutputPolicy(enabled=False)``): the forwarded payload bytes and
    the shape of ``InjectionRecord.to_dict()`` are unchanged from pre-BOD-333
    behaviour. When ``policy.enabled`` is ``True`` this phase-1 seam attaches
    a raw-artifact pointer (digest + bytes of the exact envelope sent) and a
    cache_control preservation flag; it never alters the envelope content
    (no compression engine exists yet -- PRODUCT-SCOPE phase 2).
    """
    active = policy is not None and policy.enabled

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

    pointer: RawArtifactPointer | None = None
    cache_control_preserved: bool | None = None
    if active:
        # Phase 1: no transform runs on compiled_prompt, so the "raw artifact"
        # is exactly the envelope content already assembled above -- this is
        # the lossy/rejected-transform fallback-to-raw path by construction.
        raw_bytes = compiled_prompt.encode("utf-8")
        pointer = RawArtifactPointer(digest=digest, bytes=len(raw_bytes))
        # The envelope is a plain {"role": ..., "content": str} dict; a plain
        # string body carries no cache_control block to strip, so "preserved"
        # is vacuously true for this phase-1 (no-transform) path.
        cache_control_preserved = True

    return forwarded, InjectionRecord(
        injected=True,
        pack_state=pack_state,
        pack_digest=pack_digest,
        prompt_digest=prompt_digest or digest,
        envelope_digest=digest,
        reason="injected_leading_system_envelope",
        raw_artifact_pointer=pointer,
        cache_control_preserved=cache_control_preserved,
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
    "ContextOutputPolicy",
    "InjectionRecord",
    "RawArtifactPointer",
    "envelope_digest",
    "extract_envelope",
    "inject_context_pack",
]
