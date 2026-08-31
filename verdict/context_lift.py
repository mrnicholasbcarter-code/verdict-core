"""Paired cheaper-model lift: plant a fact, pack it, score unaided vs packed."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from verdict.context_intelligence import (
    SCHEMA_VERSION,
    ContextIntelligenceError,
    Omission,
    compile_pack,
    plan_slices,
    retrieve_units,
)
from verdict.context_pack import ContextPack, ContextUnit
from verdict.live_routing import (
    LiveRoutingError,
    LiveSurfaceBlocked,
    classify_identities,
    failover_order,
    select_route,
)
from verdict.live_routing_gateway import DEFAULT_GATEWAY, execute_chat, fetch_models, probe_identity
from verdict.memory_gate import MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane, MemoryRecord

UNAIDED_PROMPT = (
    "This project stores a unique lift token in docs, code, or memory. "
    'Reply with only this JSON object and nothing else: {"lift_fact":"<exact planted token>"} '
    "using that stored token. Do not guess. Do not use markdown."
)
Conclusion = Literal["lift", "no_lift", "blocked"]
_SECRET = re.compile(
    r"(?i)(api[_-]?key|password|authorization|credential|bearer\s+|sk-[a-z0-9]{16,})"
)
_NON_CHAT = re.compile(r"(?i)(embed|whisper|tts|lyria|image|video|audio|clip|rerank|moderation)")


@dataclass(frozen=True)
class LiftReceipt:
    identity_id: str
    cost_class: str | None
    endpoint: str
    pack_digest: str | None
    unaided_passed: bool | None
    packed_passed: bool | None
    conclusion: Conclusion
    block_reason: str | None = None
    omissions: tuple[Omission, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "identity_id": self.identity_id,
            "cost_class": self.cost_class,
            "endpoint": self.endpoint,
            "pack_digest": self.pack_digest,
            "unaided_passed": self.unaided_passed,
            "packed_passed": self.packed_passed,
            "conclusion": self.conclusion,
            "block_reason": self.block_reason,
            "omissions": [
                {"category": item.category, "reason": item.reason, "ref": item.ref}
                for item in self.omissions
            ],
        }
        return _strip_secrets(payload)


def new_lift_token() -> str:
    return f"verdict-lift-{secrets.token_hex(16)}"


def unaided_prompt() -> str:
    return UNAIDED_PROMPT


def lift_check_passes(body: str, planted_token: str) -> bool:
    try:
        parsed = json.loads(body.strip())
    except json.JSONDecodeError:
        return False
    return bool(parsed == {"lift_fact": planted_token})


def plant_lift_workspace(root: Path, token: str, *, dummy_files: int = 20) -> Path:
    root = root.resolve()
    (root / "docs" / "adr").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "adr" / "ADR-LIFT-PROOF.md").write_text(
        f"# Lift proof\n\nThe unique lift token is: {token}\n", encoding="utf-8"
    )
    (root / "src" / "lift_marker.py").write_text(f'LIFT_FACT_TOKEN = "{token}"\n', encoding="utf-8")
    for index in range(dummy_files):
        (root / f"noise_{index:02d}.py").write_text(
            f"# unrelated filler module {index}\nVALUE = {index}\n", encoding="utf-8"
        )
    return root


def ingest_lift_fact(gate: MemoryGate, token: str) -> MemoryRecord:
    if _looks_secret(token) or _looks_secret(f"The unique lift token is: {token}"):
        result = gate.write(
            MemoryWriteRequest(
                namespace="patterns",
                key="rejected_secret",
                value="sk-this-should-never-be-stored-as-a-secret",
                authority="verdict-core",
                provenance={"source": "context-lift", "rejected": True},
            )
        )
        raise ContextIntelligenceError("secret_refused", result.reason or "secret write refused")
    result = gate.write(
        MemoryWriteRequest(
            namespace="patterns",
            key="lift_fact",
            value=f"The unique lift token is: {token}",
            authority="verdict-core",
            confidence=1.0,
            provenance={"source": "context-lift-plant", "kind": "synthetic"},
            source="context-lift",
        )
    )
    if not result.allowed or result.record is None:
        raise ContextIntelligenceError("secret_refused", result.reason or "ingest refused")
    return result.record


def refuse_secret_write(gate: MemoryGate, secret: str) -> None:
    result = gate.write(
        MemoryWriteRequest(
            namespace="patterns",
            key="api_key",
            value=secret,
            authority="verdict-core",
            provenance={"source": "context-lift", "attempt": "secret"},
        )
    )
    if result.allowed and result.record is not None and secret in result.record.content:
        raise ContextIntelligenceError("secret_refused", "secret was persisted")


def make_goal_unit(task: str) -> ContextUnit:
    content = task
    return ContextUnit(
        unit_id="goal:lift",
        slot_type="instructions",
        key="goal",
        content=content,
        source_uri="urn:verdict:task",
        source_digest=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
    )


def run_context_lift(
    *,
    base_url: str = DEFAULT_GATEWAY,
    proof_root: Path | None = None,
    token: str | None = None,
    plane: MemoryPlane | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    token = token or new_lift_token()
    owns_plane = plane is None
    root = (proof_root or Path.cwd()).resolve()
    if proof_root is not None:
        plant_lift_workspace(root, token)
    if owns_plane:
        db_path = root / ".verdict" / "lift-memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        plane = MemoryPlane(db_path)
    assert plane is not None
    gate = MemoryGate(plane)
    try:
        ingest_lift_fact(gate, token)
        task = "Return the unique lift token stored in this project's docs, code, or memory."
        if token in unaided_prompt() or token in task:
            raise ContextIntelligenceError(
                "invalid_pair", "planted token leaked into unaided wording"
            )
        slices = plan_slices(task, proof_root=root)
        retrieved = retrieve_units(
            slices, proof_root=root, plane=plane, required_fact=token, task=task
        )
        try:
            identities, _captured = fetch_models(base_url)
            candidates = classify_identities(identities)
            selection = select_route(candidates)
        except LiveSurfaceBlocked as exc:
            return _blocked("live_surface_blocked", str(exc), base_url, retrieved.omissions)
        except LiveRoutingError as exc:
            code = exc.code if exc.code != "no_qualified_candidate" else "no_cheaper_identity"
            return _blocked(code, str(exc), base_url, retrieved.omissions)
        identity = None
        pack = None
        state = None
        unaided_body = ""
        packed_body = ""
        last_block = "no_cheaper_identity"
        units = (make_goal_unit(task), *retrieved.units)
        cheaper = [
            candidate
            for candidate in failover_order(selection)
            if candidate.identity is not None
            and candidate.identity.cost_class in {"local", "free", "cheaper"}
            and _NON_CHAT.search(candidate.identity.identity_id) is None
        ][:20]
        for candidate in cheaper:
            current = candidate.identity
            if current is None or current.cost_class not in {"local", "free", "cheaper"}:
                continue
            if _NON_CHAT.search(current.identity_id):
                continue
            if current.context_limit is None:
                last_block = "unclassified_context_limit"
                continue
            if not probe_identity(base_url, current.identity_id):
                last_block = "live_surface_blocked"
                continue
            budget = max(64, min(current.context_limit - 256, 2048))
            try:
                pack, state = compile_pack(
                    units,
                    token_budget=budget,
                    required_fact=token,
                    candidate_id=current.identity_id,
                )
            except ContextIntelligenceError as exc:
                last_block = exc.code
                continue
            if not execute:
                identity = current
                break
            try:
                unaided_body = execute_chat(
                    base_url, current.identity_id, [{"role": "user", "content": unaided_prompt()}]
                )
                packed_body = execute_chat(base_url, current.identity_id, _packed_messages(pack))
            except LiveSurfaceBlocked:
                last_block = "live_surface_blocked"
                continue
            if not unaided_body.strip() and not packed_body.strip():
                last_block = "live_surface_blocked"
                continue
            identity = current
            break
        if identity is None or pack is None or state is None:
            return _blocked(last_block, last_block, base_url, retrieved.omissions)
        if not execute:
            receipt = LiftReceipt(
                identity_id=identity.identity_id,
                cost_class=identity.cost_class,
                endpoint=base_url,
                pack_digest=pack.digest,
                unaided_passed=None,
                packed_passed=None,
                conclusion="blocked",
                block_reason="execute_disabled",
                omissions=retrieved.omissions,
            )
            return {"receipt": receipt.to_dict(), "working_state": state.to_slots()}
        unaided_ok = lift_check_passes(unaided_body, token)
        packed_ok = lift_check_passes(packed_body, token)
        if unaided_ok is False and packed_ok is True:
            conclusion: Conclusion = "lift"
        else:
            conclusion = "no_lift"
        receipt = LiftReceipt(
            identity_id=identity.identity_id,
            cost_class=identity.cost_class,
            endpoint=base_url,
            pack_digest=pack.digest,
            unaided_passed=unaided_ok,
            packed_passed=packed_ok,
            conclusion=conclusion,
            omissions=retrieved.omissions,
        )
        return {
            "receipt": receipt.to_dict(),
            "working_state": state.to_slots(),
            "file_count": retrieved.file_count,
        }
    finally:
        gate.close()
        if owns_plane:
            plane.close()


def _packed_messages(pack: ContextPack) -> list[dict[str, str]]:
    return [{"role": "user", "content": f"{pack.compiled_prompt}\n\n{unaided_prompt()}"}]


def _looks_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


def _strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if _SECRET.search(key):
            continue
        if isinstance(value, str) and _SECRET.search(value):
            continue
        if isinstance(value, dict):
            clean[key] = _strip_secrets(value)
        elif isinstance(value, list):
            clean[key] = [
                _strip_secrets(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            clean[key] = value
    return clean


def _blocked(
    code: str,
    message: str,
    endpoint: str,
    omissions: tuple[Omission, ...],
    identity_id: str = "",
    pack_digest: str | None = None,
) -> dict[str, Any]:
    receipt = LiftReceipt(
        identity_id=identity_id,
        cost_class=None,
        endpoint=endpoint,
        pack_digest=pack_digest,
        unaided_passed=None,
        packed_passed=None,
        conclusion="blocked",
        block_reason=code,
        omissions=omissions,
    )
    return {"receipt": receipt.to_dict(), "error": message, "code": code}


__all__ = [
    "UNAIDED_PROMPT",
    "LiftReceipt",
    "ingest_lift_fact",
    "lift_check_passes",
    "new_lift_token",
    "plant_lift_workspace",
    "refuse_secret_write",
    "run_context_lift",
    "unaided_prompt",
]
