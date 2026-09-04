"""Context intelligence: deterministic slices, retrieval, and model-aware packs.

Core owns policy. Retrieval adapters supply units only. Compiling a pack never
dumps a repository or a chat transcript.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import time as unix_time
from typing import Literal

from verdict.context_pack import (
    ContextPack,
    ContextPackCompiler,
    ContextPlan,
    ContextUnit,
    estimate_tokens,
)
from verdict.memory_plane import MemoryPlane, MemorySearchResult

SCHEMA_VERSION = "context-intelligence/v1"
SliceCategory = Literal["docs", "code", "memory"]
DEFAULT_MAX_UNITS = 8
DEFAULT_MAX_FILE_BYTES = 8_192
_SKIP_DIR_PARTS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox"}
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key\s*[:=]|password\s*[:=]|bearer\s+[a-z0-9._~+/=-]{16,}|sk-[a-z0-9]{20,})"
)
_STOP = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "your",
        "project",
        "stored",
        "store",
        "return",
        "json",
        "only",
        "reply",
        "using",
        "exact",
        "planted",
        "unique",
        "docs",
        "code",
        "memory",
        "the",
        "and",
        "for",
        "into",
    }
)


class ContextIntelligenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RetrievalSlice:
    slice_id: str
    category: SliceCategory
    query: str
    root: str | None = None
    max_units: int = DEFAULT_MAX_UNITS


@dataclass(frozen=True)
class Omission:
    category: str
    reason: str
    ref: str | None = None


@dataclass(frozen=True)
class WorkingState:
    goal: str
    slices: tuple[str, ...]
    pack_digest: str | None = None
    required_fact_kept: bool = False
    omissions: tuple[Omission, ...] = ()

    def to_slots(self) -> dict[str, str]:
        return {
            "goal": self.goal,
            "slices": ",".join(self.slices),
            "pack_digest": self.pack_digest or "",
            "required_fact_kept": "true" if self.required_fact_kept else "false",
            "omissions": ",".join(f"{item.category}:{item.reason}" for item in self.omissions),
        }


@dataclass(frozen=True)
class RetrievalResult:
    units: tuple[ContextUnit, ...]
    omissions: tuple[Omission, ...]
    working_state: WorkingState
    file_count: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _query_terms(query: str) -> tuple[str, ...]:
    terms = [part.lower() for part in re.findall(r"[A-Za-z0-9_-]{4,}", query)]
    return tuple(term for term in terms if term not in _STOP)


def _looks_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


def _skip_path(path: Path) -> bool:
    return any(part in _SKIP_DIR_PARTS for part in path.parts)


def plan_slices(task: str, *, proof_root: Path) -> tuple[RetrievalSlice, ...]:
    root = proof_root.resolve()
    query = task.strip() or "lift token"
    return (
        RetrievalSlice("docs-adr", "docs", query, root=str(root / "docs" / "adr")),
        RetrievalSlice("code-markers", "code", query, root=str(root)),
        RetrievalSlice("memory-search", "memory", query),
    )


def retrieve_units(
    slices: Sequence[RetrievalSlice],
    *,
    proof_root: Path,
    plane: MemoryPlane | None = None,
    required_fact: str | None = None,
    task: str = "",
) -> RetrievalResult:
    root = proof_root.resolve()
    units: list[ContextUnit] = []
    omissions: list[Omission] = []
    file_count = _count_files(root)
    for item in slices:
        if item.max_units > 32 or _is_dump_slice(item, root):
            raise ContextIntelligenceError(
                "repo_dump_refused", f"slice {item.slice_id} would dump the repository"
            )
        if item.category == "docs":
            found = _retrieve_docs(item, root, required_fact=required_fact)
        elif item.category == "code":
            found = _retrieve_code(item, root, required_fact=required_fact)
        elif item.category == "memory":
            found = _retrieve_memory(item, plane, required_fact=required_fact)
        else:
            found = []
        if not found:
            reason = "not_found"
            missing_docs = item.category == "docs" and not (root / "docs" / "adr").is_dir()
            if (item.category == "memory" and plane is None) or missing_docs:
                reason = "no_default_location"
            omissions.append(Omission(item.category, reason, item.root))
        units.extend(found)
    if required_fact and not any(required_fact in unit.content for unit in units):
        omissions.append(Omission("required_fact", "not_found", None))
    state = WorkingState(
        goal=task or "retrieve",
        slices=tuple(item.slice_id for item in slices),
        omissions=tuple(omissions),
    )
    return RetrievalResult(tuple(units), tuple(omissions), state, file_count=file_count)


def compile_pack(
    units: Sequence[ContextUnit],
    *,
    token_budget: int,
    required_fact: str,
    candidate_id: str,
    compaction: bool = False,
) -> tuple[ContextPack, WorkingState]:
    if not required_fact.strip():
        raise ContextIntelligenceError("required_fact_missing", "required fact is empty")
    now = unix_time()
    prepared = []
    for unit in units:
        if required_fact in unit.content:
            prepared.append(replace(unit, confidence=1.0, created_at=now + 1_000_000))
        else:
            prepared.append(replace(unit, confidence=min(unit.confidence, 0.2), created_at=now))
    if compaction:
        prepared = _compact_optional(
            prepared, required_fact=required_fact, token_budget=token_budget
        )
    if not any(required_fact in unit.content for unit in prepared):
        raise ContextIntelligenceError(
            "required_fact_missing", "required fact is not in retrieved units"
        )
    required_cost = sum(
        estimate_tokens(unit.content) for unit in prepared if required_fact in unit.content
    )
    if required_cost >= token_budget:
        raise ContextIntelligenceError(
            "required_fact_omitted", "token budget cannot hold the required fact"
        )
    compiler = ContextPackCompiler(default_token_budget=token_budget)
    plan = ContextPlan(
        plan_id=f"lift:{candidate_id}",
        candidate_id=candidate_id,
        token_budget=token_budget,
        output_token_reserve=0,
        tool_token_reserve=0,
    )
    pack = compiler.compile_units(tuple(prepared), plan)
    if required_fact not in pack.compiled_prompt:
        raise ContextIntelligenceError(
            "required_fact_omitted", "compiled pack omitted the required fact"
        )
    state = WorkingState(
        goal="compile",
        slices=(),
        pack_digest=pack.digest,
        required_fact_kept=True,
        omissions=tuple(
            Omission("pack", decision.reason, decision.unit_id)
            for decision in pack.decisions
            if decision.action == "exclude"
        ),
    )
    return pack, state


def _is_dump_slice(item: RetrievalSlice, proof_root: Path) -> bool:
    if not item.root:
        return False
    resolved = Path(item.root).resolve()
    if resolved in {Path("/"), Path.home().resolve()}:
        return True
    return item.category in {"docs", "code"} and resolved == proof_root and not item.query.strip()


def _count_files(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file() and not _skip_path(path))


def _matches(text: str, query: str, required_fact: str | None) -> bool:
    if required_fact and required_fact in text:
        return True
    lowered = text.lower()
    terms = _query_terms(query)
    return any(term in lowered for term in terms)


def _unit(
    *, slot_type: str, key: str, content: str, source_uri: str, confidence: float = 1.0
) -> ContextUnit | None:
    if _looks_secret(content):
        return None
    return ContextUnit(
        unit_id=_digest_text(source_uri + key)[:24],
        slot_type=slot_type,  # type: ignore[arg-type]
        key=key,
        content=content,
        source_uri=source_uri,
        source_digest=_digest_text(content),
        observed_at=_now_iso(),
        trust="local-observation",
        confidence=confidence,
    )


def _retrieve_docs(
    item: RetrievalSlice, proof_root: Path, *, required_fact: str | None
) -> list[ContextUnit]:
    adr = Path(item.root).resolve() if item.root else proof_root / "docs" / "adr"
    if not adr.is_dir():
        adr = proof_root / "docs" / "adr"
    if not adr.is_dir():
        return []
    found: list[ContextUnit] = []
    for path in sorted(adr.rglob("*.md")):
        if _skip_path(path) or not path.is_file():
            continue
        payload = path.read_text(encoding="utf-8", errors="replace")[:DEFAULT_MAX_FILE_BYTES]
        if not _matches(payload, item.query, required_fact):
            continue
        unit = _unit(
            slot_type="evidence",
            key=f"docs:{path.name}",
            content=payload,
            source_uri=str(path.relative_to(proof_root) if proof_root in path.parents else path),
        )
        if unit is not None:
            found.append(unit)
        if len(found) >= item.max_units:
            break
    return found


def _retrieve_code(
    item: RetrievalSlice, proof_root: Path, *, required_fact: str | None
) -> list[ContextUnit]:
    root = Path(item.root).resolve() if item.root else proof_root
    if not root.is_dir():
        return []
    found: list[ContextUnit] = []
    for path in sorted(root.rglob("*.py")):
        if _skip_path(path) or not path.is_file():
            continue
        payload = path.read_text(encoding="utf-8", errors="replace")[:DEFAULT_MAX_FILE_BYTES]
        if not _matches(payload, item.query, required_fact):
            continue
        rel = str(
            path.relative_to(proof_root)
            if proof_root in path.parents or path == proof_root
            else path
        )
        unit = _unit(slot_type="evidence", key=f"code:{path.name}", content=payload, source_uri=rel)
        if unit is not None:
            found.append(unit)
        if len(found) >= item.max_units:
            break
    return found


def _retrieve_memory(
    item: RetrievalSlice, plane: MemoryPlane | None, *, required_fact: str | None
) -> list[ContextUnit]:
    if plane is None:
        return []
    ranked: list[MemorySearchResult] = plane.search_ranked(item.query, limit=item.max_units)
    found: list[ContextUnit] = []
    for result in ranked:
        content = result.record.content
        if not _matches(content, item.query, required_fact):
            continue
        if result.stale:
            continue
        if result.record.namespace == "memory_gate_events" or result.record.key.startswith(
            "gate_event"
        ):
            continue
        unit = _unit(
            slot_type="memory",
            key=f"memory:{result.record.key}",
            content=content,
            source_uri=f"memory:{result.record.record_id}",
            confidence=max(0.1, min(1.0, 1.0 / (result.rank or 1))),
        )
        if unit is not None:
            found.append(unit)
    if required_fact and not any(required_fact in unit.content for unit in found):
        for record in plane.records():
            if required_fact in record.content:
                unit = _unit(
                    slot_type="memory",
                    key=f"memory:{record.key}",
                    content=record.content,
                    source_uri=f"memory:{record.record_id}",
                )
                if unit is not None:
                    found.append(unit)
                break
    return found[: item.max_units]


def _compact_optional(
    units: Sequence[ContextUnit], *, required_fact: str, token_budget: int
) -> list[ContextUnit]:
    kept: list[ContextUnit] = []
    used = 0
    for unit in units:
        required = required_fact in unit.content or unit.slot_type in {"instructions", "policy"}
        content = unit.content
        if not required and estimate_tokens(content) > 80:
            content = content[:160] + "\n[summarized]"
            unit = ContextUnit(
                unit_id=unit.unit_id,
                slot_type=unit.slot_type,
                key=unit.key,
                content=content,
                source_uri=unit.source_uri,
                source_digest=_digest_text(content),
                observed_at=unit.observed_at,
                trust=unit.trust,
                confidence=unit.confidence,
            )
        cost = estimate_tokens(unit.content)
        if not required and used + cost > token_budget:
            continue
        kept.append(unit)
        used += cost
    return kept


__all__ = [
    "SCHEMA_VERSION",
    "ContextIntelligenceError",
    "Omission",
    "RetrievalResult",
    "RetrievalSlice",
    "WorkingState",
    "compile_pack",
    "plan_slices",
    "retrieve_units",
]
