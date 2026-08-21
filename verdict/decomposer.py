"""Decompose an objective into verifiable :class:`~verdict.work_unit.WorkUnit` slices.

This is the expensive half of the loop: one orchestrator call produces the plan,
after which cheap routes execute it.  The orchestrator must emit, for every
slice, both the files it owns and the command that proves it done.  A slice that
fails :class:`WorkUnit` validation is a *decomposition failure* and is reported
as one — the plan is never silently repaired, because a plan that cannot state
its own check is the thing worth measuring.

The orchestrator's token usage is recorded so the expensive/cheap split in the
final report comes from measured counts rather than estimates.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict.patch_executor import (
    DEFAULT_BASE_URL,
    PatchExecutorError,
    TokenUsage,
    extract_content,
    parse_usage,
)
from verdict.probes import ProbeTransport, openai_probe_transport
from verdict.work_unit import WorkUnit, WorkUnitError, parse_work_units

DEFAULT_ORCHESTRATOR_MODEL = "cc/claude-opus-5"

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

DECOMPOSITION_SYSTEM_PROMPT = (
    "You decompose a software objective into independent, verifiable work units. "
    "Reply with a JSON array and nothing else.\n\n"
    "Each element must be an object with exactly these keys:\n"
    '  "unit_id": short stable kebab-case id, unique in the array\n'
    '  "objective": what this unit alone must accomplish\n'
    '  "owned_files": list of repository-relative paths this unit may modify. '
    "No two units may own the same file. No absolute paths, no `..`.\n"
    '  "verification_command": argv list (e.g. ["ruff","check","path.py"]) that '
    "exits zero exactly when this unit is done, and is scoped to this unit's "
    "files so it cannot pass on another unit's work.\n"
    '  "context": optional extra detail for the executor, or ""\n\n'
    "Prefer many small single-file units over few large ones. Every unit must be "
    "executable without reading another unit's output. If you cannot state a "
    "runnable verification command for a unit, do not emit that unit."
)


class DecompositionError(RuntimeError):
    """Raised when an objective could not be decomposed into valid work units."""


@dataclass(frozen=True)
class DecompositionConfig:
    """Settings for the single orchestrator call.

    ``api_key`` is explicit and never read from the environment or logged.
    """

    model: str = DEFAULT_ORCHESTRATOR_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    timeout_seconds: float = 180.0
    max_tokens: int = 8192
    temperature: float = 0.0
    max_units: int = 64
    max_response_bytes: int = 1_048_576


@dataclass(frozen=True)
class DecompositionResult:
    """A validated plan plus the measured cost of producing it."""

    units: tuple[WorkUnit, ...]
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "unit_count": len(self.units),
            "units": [unit.to_dict() for unit in self.units],
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
        }


class Decomposer:
    """Ask the orchestrator route for a plan and validate every slice of it."""

    def __init__(
        self, config: DecompositionConfig | None = None, *, transport: ProbeTransport | None = None
    ) -> None:
        self.config = config or DecompositionConfig()
        self._transport = transport or openai_probe_transport(
            self.config.base_url,
            api_key=self.config.api_key,
            opener=urllib.request.urlopen,
            max_response_bytes=self.config.max_response_bytes,
        )

    def decompose(
        self, objective: str, *, repo_root: str | Path, evidence: str = ""
    ) -> DecompositionResult:
        """Return validated work units for ``objective``, or raise.

        Raises:
            DecompositionError: on transport failure, unparseable output, or any
                slice that is not a valid :class:`WorkUnit`.
        """
        if not isinstance(objective, str) or not objective.strip():
            raise DecompositionError("objective must be a non-empty string")

        started = time.monotonic()
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": DECOMPOSITION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_decomposition_prompt(objective, repo_root, evidence),
                },
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        try:
            response = self._transport(self.config.model, payload, self.config.timeout_seconds)
        except Exception as exc:
            raise DecompositionError(
                f"orchestrator transport error: {type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(response, Mapping):
            raise DecompositionError("orchestrator returned a non-object response")
        status = response.get("status_code")
        if isinstance(status, int) and not 200 <= status < 300:
            raise DecompositionError(f"orchestrator returned HTTP {status}")
        body = response.get("body")
        if not isinstance(body, Mapping):
            raise DecompositionError("orchestrator response body is not an object")

        usage = parse_usage(body.get("usage"))
        try:
            content = extract_content(body)
        except PatchExecutorError as exc:
            raise DecompositionError(str(exc)) from exc

        units = parse_decomposition(content, max_units=self.config.max_units)
        return DecompositionResult(
            units=units,
            model=self.config.model,
            usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def parse_decomposition(content: str, *, max_units: int = 64) -> tuple[WorkUnit, ...]:
    """Validate an orchestrator response into work units.

    Every failure mode here — unparseable JSON, a malformed slice, a missing
    verification command, two units claiming the same file — is a decomposition
    failure, not something to route around.
    """
    if not isinstance(content, str) or not content.strip():
        raise DecompositionError("orchestrator returned an empty response")

    raw = _strip_fence(content.strip())
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(raw)
        if not match:
            raise DecompositionError("orchestrator response is not JSON") from None
        try:
            decoded = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise DecompositionError(f"orchestrator response is not valid JSON: {exc}") from exc

    if isinstance(decoded, Mapping):
        for key in ("units", "work_units", "slices"):
            if key in decoded:
                decoded = decoded[key]
                break

    if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes)):
        raise DecompositionError("orchestrator response is not a JSON array of work units")
    if len(decoded) > max_units:
        raise DecompositionError(
            f"decomposition produced {len(decoded)} units, above the limit of {max_units}"
        )

    try:
        units = parse_work_units(decoded)
    except WorkUnitError as exc:
        raise DecompositionError(f"invalid work unit in decomposition: {exc}") from exc

    owners: dict[str, str] = {}
    for unit in units:
        for path in unit.owned_files:
            previous = owners.get(path)
            if previous is not None:
                raise DecompositionError(
                    f"units {previous!r} and {unit.unit_id!r} both claim {path!r}; "
                    "work units must own disjoint files"
                )
            owners[path] = unit.unit_id
    return units


def build_decomposition_prompt(objective: str, repo_root: str | Path, evidence: str = "") -> str:
    """Render the orchestrator's user message."""
    sections = [f"Repository: {Path(repo_root)}", "", f"Objective: {objective}"]
    if evidence:
        sections += [
            "",
            "Current state of the objective (authoritative — decompose against this, "
            "not against assumptions):",
            evidence,
        ]
    sections += ["", "Reply with the JSON array of work units only."]
    return "\n".join(sections)


def _strip_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    body = text.split("\n", 1)[1] if "\n" in text else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()


__all__ = [
    "DECOMPOSITION_SYSTEM_PROMPT",
    "DEFAULT_ORCHESTRATOR_MODEL",
    "Decomposer",
    "DecompositionConfig",
    "DecompositionError",
    "DecompositionResult",
    "build_decomposition_prompt",
    "parse_decomposition",
]
