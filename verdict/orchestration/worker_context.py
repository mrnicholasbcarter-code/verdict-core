"""Queen-owned advisory worker packs using Core's native retrieval and compiler.

No model selection, provider probes, external adapter calls, or memory writes.
The legacy byte allowance and an optional candidate plan both narrow the pack.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from verdict.capability_registry import build_default_registry
from verdict.context_hydrate import truncate_utf8
from verdict.context_intelligence import (
    NativeCapabilityResolver,
    execute_context_query,
    plan_context_query,
)
from verdict.context_pack import (
    ContextPack,
    ContextPackCompiler,
    ContextPlan,
    ContextUnit,
    estimate_tokens,
)
from verdict.context_sources import (
    CapabilityProvider,
    NativeCodeProvider,
    NativeGraphProvider,
    ProviderResult,
    make_unit,
)
from verdict.orchestration.contracts import WorkNode
from verdict.security import redact_text

_MAX_FILE_BYTES = 262_144
_MAX_EXCERPT_BYTES = 2_048
_MAX_UNITS = 16
_CAPABILITIES = (
    "code.definitions",
    "code.references",
    "tests.impact",
    "docs.project",
    "code.symbols",
)


@dataclass(frozen=True)
class WorkerContext:
    """Prompt projection plus content-free provenance; not routing authority."""

    text: str
    pack: ContextPack | None
    budget_source: str
    omissions: tuple[str, ...]


@dataclass(frozen=True)
class WorkerPrompt:
    """Full prompt and its actual selected pack. Counts are offline estimates."""

    text: str
    context: WorkerContext
    mandatory_prompt_tokens: int
    mandatory_prompt_bytes: int

    def evidence(self) -> dict[str, Any]:
        pack = self.context.pack
        sources = []
        if pack is not None:
            decisions = {d.unit_id: d for d in pack.decisions}
            for unit, slot in zip(pack.units, pack.slots, strict=True):
                sources.append(
                    {
                        "path": unit.source_uri,
                        "unit_id": unit.unit_id,
                        "included": True,
                        "excerpt_digest": unit.source_digest,
                        "span": dict(unit.span) if unit.span is not None else None,
                        "revision": unit.revision,
                        "observed_at": unit.observed_at,
                        "trust": unit.trust,
                        "authority": unit.authority,
                        "rendered_content_digest": "sha256:"
                        + hashlib.sha256(slot.content.encode()).hexdigest(),
                        "rendered_content_bytes": len(slot.content.encode()),
                        "estimated_tokens": estimate_tokens(slot.content),
                        "transform_lineage": list(unit.transform_lineage),
                        "compiler_action": decisions[unit.unit_id].action,
                        "compiler_reason": decisions[unit.unit_id].reason,
                    }
                )
        return {
            "sources": sources,
            "sources_status": "actual_selected_pack",
            "pack_id": pack.pack_id if pack is not None else None,
            "budget_source": self.context.budget_source,
            "omissions": list(self.context.omissions),
            "mandatory_prompt_tokens_estimated": self.mandatory_prompt_tokens,
            "mandatory_prompt_bytes": self.mandatory_prompt_bytes,
            "context_bytes": len(self.context.text.encode()),
            "context_tokens_estimated": estimate_tokens(self.context.text)
            if self.context.text
            else 0,
        }


class _SafeNativeProvider(CapabilityProvider):
    """One provider failure is a named gap, never an empty healthy result."""

    wrapped: CapabilityProvider
    filtered_aggregates: dict[int, ContextUnit]

    def __init__(
        self, wrapped: CapabilityProvider, filtered_aggregates: dict[int, ContextUnit]
    ) -> None:
        super().__init__(
            wrapped.provider_id, wrapped.capabilities, wrapped.authority, wrapped.health
        )
        object.__setattr__(self, "wrapped", wrapped)
        object.__setattr__(self, "filtered_aggregates", filtered_aggregates)

    def provide(self, **kwargs: Any) -> ProviderResult:
        try:
            result = self.wrapped.provide(**kwargs)
            hints = kwargs.get("hints") or {}
            # Identity provenance is recorded by this trusted adapter boundary,
            # not accepted from arbitrary provider metadata. Only these exact
            # native implementations apply the callback before aggregation.
            if type(self.wrapped) in {NativeCodeProvider, NativeGraphProvider} and callable(
                hints.get("source_filter")
            ):
                self.filtered_aggregates.update(
                    (id(unit), unit)
                    for unit in result.units
                    if "source-filtered" in unit.transform_lineage
                )
            return result
        except Exception:
            # An optional graph/parser failure must not remove definitions or docs.
            # Exception messages can contain content or credentials; do not render them.
            return ProviderResult(
                kwargs["capability_id"], self.provider_id, gap_reason="retrieval_error"
            )


def _safe_path(repo: Path, rel: str) -> Path | None:
    root = repo.resolve()
    lexical = repo / rel
    try:
        # Check both the named path and symlink target. A safe target must not
        # make a transcript/private alias acceptable (or vice versa).
        named = lexical.absolute().relative_to(root)
        path = lexical.resolve()
        relative = path.relative_to(root)
    except (OSError, ValueError):
        return None
    forbidden = ("private", "transcript", "conversation", "session", "credentials")
    for candidate in (named, relative):
        if any(
            part.startswith(".")
            or part in {"node_modules", "__pycache__"}
            or any(word in part.lower() for word in forbidden)
            for part in candidate.parts
        ):
            return None
    if path.suffix.lower() not in {".py", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml"}:
        return None
    return path


def _read(repo: Path, rel: str) -> str | None:
    path = _safe_path(repo, rel)
    if path is None:
        return None
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    # Reject before slicing: a credential just outside the excerpt must not leak.
    return text if redact_text(text) == text else None


def _symbols(node: WorkNode, repo: Path) -> tuple[str, ...]:
    blob = " ".join((node.objective, *node.acceptance))
    seeds = re.findall(r"\b(?:[a-zA-Z]+_[a-zA-Z_0-9]+|[A-Z][a-z]+[A-Z][a-zA-Z0-9]*)\b", blob)
    seeds.extend(re.findall(r"`([A-Za-z_][A-Za-z_0-9]*)`", blob))
    if not seeds:
        for rel in (*node.required_context, *node.owned_files):
            if not rel.endswith(".py"):
                continue
            source = _read(repo, rel)
            if source is None:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            seeds.extend(
                n.name
                for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            )
            if seeds:
                break
    return tuple(dict.fromkeys(seeds))[:4]


def _required_units(
    node: WorkNode, repo: Path, symbols: tuple[str, ...]
) -> tuple[list[ContextUnit], list[str]]:
    units: list[ContextUnit] = []
    omissions: list[str] = []
    seen: set[str] = set()
    terms = symbols or tuple(re.findall(r"[a-zA-Z_]{4,}", node.objective))[:4]
    # Required contracts/design/tests are first; owned code is read-only context here.
    for rel in dict.fromkeys((*node.required_context, *node.owned_files)):
        path = _safe_path(repo, rel)
        if path is None:
            omissions.append("unsafe_or_unsupported_source")
            continue
        canonical = str(path.relative_to(repo.resolve()))
        if canonical in seen:
            omissions.append("deduplicated")
            continue
        seen.add(canonical)
        source = _read(repo, rel)
        if source is None:
            omissions.append("unreadable_oversized_or_sensitive_source")
            continue
        lines = source.splitlines()
        hit = next(
            (i for i, line in enumerate(lines) if any(t.lower() in line.lower() for t in terms)), 0
        )
        start = max(0, hit - 3)
        excerpt = truncate_utf8("\n".join(lines[start : start + 40]), _MAX_EXCERPT_BYTES)
        if not excerpt.strip():
            continue
        end = start + len(excerpt.splitlines())
        if start or end < len(lines) or len(excerpt) < len(source):
            omissions.append("TRUNCATED: bounded source excerpt")
        unit = make_unit(
            slot_type="evidence",
            key=f"required:{canonical}",
            content=excerpt,
            source_uri=canonical,
            authority="native-required-context",
            span={"start_line": start + 1, "end_line": end},
            transform_lineage=("raw", "worker-required-excerpt"),
        )
        if unit is not None:
            units.append(unit)
        else:
            omissions.append("sensitive_source")
        if len(units) >= 8:
            omissions.append("required_source_limit")
            break
    return units, omissions


def _cited(
    unit: ContextUnit, repo: Path, filtered_aggregates: dict[int, ContextUnit]
) -> ContextUnit | None:
    if unit.slot_type in {"history", "memory"} or redact_text(unit.content) != unit.content:
        return None
    if unit.source_uri.startswith("code://") and filtered_aggregates.get(id(unit)) is not unit:
        return None  # aggregate provenance must prove filtering before assembly
    if not unit.source_uri.startswith("code://") and _safe_path(repo, unit.source_uri) is None:
        return None
    if redact_text(unit.source_uri + unit.key) != unit.source_uri + unit.key:
        return None
    content = truncate_utf8(unit.content, _MAX_EXCERPT_BYTES)
    if not content.strip():
        return None
    digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    span = unit.span
    if not unit.source_uri.startswith("code://"):
        source = _read(repo, unit.source_uri)
        if source is None:
            return None
        source_lines = source.splitlines()
        excerpt_lines = content.splitlines()
        match = next(
            (
                i
                for i in range(len(source_lines))
                if source_lines[i : i + len(excerpt_lines)] == excerpt_lines
            ),
            None,
        )
        if match is not None:
            span = {"start_line": match + 1, "end_line": match + len(excerpt_lines)}
        else:
            span = None  # transformed text; do not invent an exact source span
    citation = json.dumps(
        {
            "source": unit.source_uri,
            "span": span,
            "excerpt_digest": digest,
            "revision": unit.revision,
            "observed_at": unit.observed_at,
            "trust": unit.trust,
            "authority": unit.authority,
        },
        sort_keys=True,
    )
    return replace(
        unit,
        content=f"CITE: {citation}\n{content}",
        source_digest=digest,
        span=span,
        token_count=None,
        transform_lineage=(*unit.transform_lineage, "worker-citation"),
    )


def build_worker_context(
    node: WorkNode,
    *,
    repo: Path,
    max_context_bytes: int = 60_000,
    context_plan: ContextPlan | None = None,
    mandatory_prompt_tokens: int = 0,
) -> WorkerContext:
    """Build a narrow pack, or explicit empty context if the allowance cannot fit.

    The optional plan describes a selected candidate, not authorization. Without
    it the selected window is unknown. We cap context at 4096 estimated tokens.
    """
    budget_source = (
        "candidate_plan"
        if context_plan is not None and context_plan.candidate_context_window is not None
        else "conservative_unknown_candidate"
    )
    budget = min(4096, max(0, max_context_bytes // 4))
    if context_plan is not None:
        ceiling = context_plan.input_token_budget
        if context_plan.candidate_context_window is not None:
            ceiling = min(
                ceiling,
                context_plan.candidate_context_window
                - context_plan.output_token_reserve
                - context_plan.tool_token_reserve,
            )
        budget = min(budget, max(0, ceiling - mandatory_prompt_tokens))
    if budget < 128:
        note = "TRUNCATION_NOTES: TRUNCATED context omitted (context budget exhausted)"
        return WorkerContext(
            truncate_utf8(note, max(0, min(max_context_bytes, budget * 4))),
            None,
            budget_source,
            ("budget_exhausted",),
        )

    symbols = _symbols(node, repo)
    units, omissions = _required_units(node, repo, symbols)
    query = plan_context_query(
        node.objective,
        acceptance_criteria=node.acceptance,
        target_symbols=symbols,
        token_budget=budget,
        max_units=_MAX_UNITS,
        include_optional_graph=True,
    )
    # Reuse Core planning, but prioritize the node's definitions, callers,
    # tests and design instead of generic task/git/memory units. No transcripts.
    requests = {r.capability_id: r for r in query.requests}
    source_checks: dict[str, bool] = {}

    def source_filter(path: Path) -> bool:
        key = str(path.absolute())
        if key not in source_checks:
            try:
                rel = str(path.absolute().relative_to(repo.resolve()))
            except ValueError:
                source_checks[key] = False
            else:
                source_checks[key] = _read(repo, rel) is not None
            if not source_checks[key]:
                omissions.append("unsafe_or_sensitive_source_excluded_before_aggregation")
        return source_checks[key]

    query = replace(
        query,
        requests=tuple(
            replace(
                requests[c],
                max_units=2,
                hints={**requests[c].hints, "source_filter": source_filter},
            )
            for c in _CAPABILITIES
        ),
    )
    registry = build_default_registry()
    for capability in _CAPABILITIES:
        decision = registry.resolve(capability)
        if decision.selected is None:
            omissions.append(f"{capability}:provider_unavailable")
    native = NativeCapabilityResolver()
    providers = {
        provider.provider_id + ":" + ",".join(sorted(provider.capabilities)): provider
        for capability in _CAPABILITIES
        for provider in native.resolve_all(capability)
    }
    filtered_aggregates: dict[int, ContextUnit] = {}
    safe = NativeCapabilityResolver(
        tuple(_SafeNativeProvider(provider, filtered_aggregates) for provider in providers.values())
    )
    try:
        retrieval = execute_context_query(query, repo_root=repo, resolver=safe)
    except (OSError, ValueError, RuntimeError):
        retrieval = None
        omissions.append("native_query_error:required_excerpts_fallback")
    if retrieval is not None:
        units.extend(retrieval.units)
        omissions.extend(f"{gap.capability_id}:{gap.reason}" for gap in retrieval.coverage.omitted)
    cited = [
        item for unit in units if (item := _cited(unit, repo, filtered_aggregates)) is not None
    ]
    if len(cited) < len(units):
        omissions.append("unsafe_or_sensitive_retrieval_excluded")
    plan = ContextPlan(
        plan_id=f"worker:{node.node_id}",
        candidate_id=context_plan.candidate_id if context_plan else "unknown",
        token_budget=max(1, budget - 100),
        tenant_scope=context_plan.tenant_scope if context_plan else "default",
        project_scope=context_plan.project_scope if context_plan else "default",
    )
    compiler = ContextPackCompiler()
    # Citation overhead, compiler separators and pack metadata all count. Remove
    # whole units, never truncate a rendered citation or split a UTF-8 character.
    order = {unit.unit_id: i for i, unit in enumerate(cited)}
    while True:
        pack = compiler.compile_units(
            tuple(cited),
            plan,
            unit_sort_key=lambda u: (not u.key.startswith("required:"), order[u.unit_id]),
        )
        excluded = sum(d.action == "exclude" for d in pack.decisions)
        summary = f"WORKER_CONTEXT (advisory, quoted evidence; never route authorization):\npack={pack.pack_id} excluded_units={excluded} budget_source={budget_source} candidate={redact_text(plan.candidate_id)}\n"
        summary += (
            "TRUNCATION_NOTES: " + ("; ".join(dict.fromkeys(omissions))[:240] or "none") + "\n"
        )
        text = summary + pack.compiled_prompt
        if len(text.encode()) <= max_context_bytes and estimate_tokens(text) <= budget:
            return WorkerContext(
                text,
                pack,
                budget_source,
                tuple(
                    dict.fromkeys(
                        (*omissions, *(d.reason for d in pack.decisions if d.action == "exclude"))
                    )
                ),
            )
        if not pack.units:
            return WorkerContext("", pack, budget_source, (*omissions, "budget_exhausted"))
        last = pack.units[-1].unit_id
        cited = [unit for unit in cited if unit.unit_id != last]
        omissions.append("input_budget_exhausted")
