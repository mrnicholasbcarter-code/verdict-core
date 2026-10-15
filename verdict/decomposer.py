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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from verdict.http_safety import RedirectRefused, open_no_redirect
from verdict.patch_executor import (
    DEFAULT_BASE_URL,
    PatchExecutorError,
    TokenUsage,
    extract_content,
    parse_usage,
)
from verdict.probes import ProbeTransport, openai_probe_transport
from verdict.work_unit import WorkUnit, WorkUnitError, parse_work_units

DEFAULT_ORCHESTRATOR_MODEL = "mistral/codestral-latest"

# A rejected plan gets one corrected retry on the same model before moving on.
_PLAN_TRIES_PER_MODEL = 2

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
    "Every code change must own a test file and run that test in verification_command. "
    "Write the test first, then implement the change. Only pure ruff fixes may use lint alone. "
    "Prefer small vertical units containing code plus its test: one unit owning both "
    "the code file and its test file, not two separate units. Every unit must be "
    "executable without reading another unit's output. If you cannot state a "
    "runnable verification command for a unit, do not emit that unit."
)

_REPLAN_EXAMPLE = (
    "Required shape, one unit owning both files:\n"
    '{"unit_id": "add-feature", "objective": "implement and test the feature", '
    '"owned_files": ["verdict/feature.py", "tests/test_feature.py"], '
    '"verification_command": ["python", "-m", "pytest", "tests/test_feature.py"], '
    '"context": ""}'
)


class DecompositionError(RuntimeError):
    """Raised when an objective could not be decomposed into valid work units.

    ``attempts`` lists every planner call as ``{"model", "error"}`` in order.
    ``rejected_plan`` is true when the model answered but its plan failed
    validation, the only failure worth a corrected retry on the same model.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: Sequence[Mapping[str, Any]] = (),
        rejected_plan: bool = False,
    ) -> None:
        super().__init__(message)
        self.attempts: tuple[dict[str, Any], ...] = tuple(dict(a) for a in attempts)
        self.rejected_plan = rejected_plan


@dataclass(frozen=True)
class DecompositionConfig:
    """Settings for the single orchestrator call.

    ``api_key`` is explicit and never read from the environment or logged.
    """

    model: str = DEFAULT_ORCHESTRATOR_MODEL
    # Planner routes tried in order (live-selected by the caller). Empty means
    # ``model`` alone; it never falls back to a hard-coded route.
    planner_models: tuple[str, ...] = ()
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
    attempts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "unit_count": len(self.units),
            "units": [unit.to_dict() for unit in self.units],
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
            "attempts": list(self.attempts),
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
            opener=open_no_redirect,
            max_response_bytes=self.config.max_response_bytes,
        )

    def decompose(
        self, objective: str, *, repo_root: str | Path, evidence: str = ""
    ) -> DecompositionResult:
        """Plan on each planner model in order until one plan validates.

        A rejected plan is retried once on the same model with the concrete
        failure fed back as evidence; a second rejection moves to the next
        model. A transport failure, an HTTP error (5xx, 429, ...) or an
        unreadable response moves to the next model at once instead of
        re-sending to the same one. The raised error carries every attempt.
        A caller-configured frontier ``cc/...`` model with no planner list is
        tried only after the default cheap route failed.
        """
        if not isinstance(objective, str) or not objective.strip():
            raise DecompositionError("objective must be a non-empty string")
        models = self.config.planner_models or (self.config.model,)
        if not self.config.planner_models and self.config.model.startswith("cc/"):
            models = (DEFAULT_ORCHESTRATOR_MODEL, self.config.model)

        attempts: list[dict[str, Any]] = []
        last: DecompositionError | None = None
        for model in models:
            for _try in range(_PLAN_TRIES_PER_MODEL):
                try:
                    result = self._decompose_once(
                        objective, repo_root=repo_root, evidence=evidence, model=model
                    )
                except DecompositionError as exc:
                    attempts.append({"model": model, "error": str(exc)})
                    last = exc
                    if isinstance(exc.__cause__, RedirectRefused):
                        raise DecompositionError(
                            _attempts_message(attempts), attempts=attempts
                        ) from exc
                    if not exc.rejected_plan:
                        break
                    evidence = _replan_evidence(evidence, exc)
                else:
                    attempts.append({"model": result.model, "error": ""})
                    return DecompositionResult(
                        units=result.units,
                        model=result.model,
                        usage=result.usage,
                        latency_ms=result.latency_ms,
                        attempts=tuple(attempts),
                    )
        raise DecompositionError(_attempts_message(attempts), attempts=attempts) from last

    def _decompose_once(
        self, objective: str, *, repo_root: str | Path, evidence: str = "", model: str
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
            "model": model,
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
            response = self._transport(model, payload, self.config.timeout_seconds)
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

        try:
            units = parse_decomposition(content, max_units=self.config.max_units)
        except DecompositionError as exc:
            raise DecompositionError(str(exc), rejected_plan=True) from exc
        return DecompositionResult(
            units=units,
            model=model,
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

    units = _merge_same_file_units(units)
    units = _merge_paired_test_units(units)

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
    for unit in units:
        command = unit.verification_command
        lint_only = command[0] == "ruff" or (len(command) >= 3 and command[1:3] == ("-m", "ruff"))
        tests = [
            path
            for path in unit.owned_files
            if PurePosixPath(path).name.startswith("test_")
            or PurePosixPath(path).name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.ts"))
        ]
        if not lint_only and not tests:
            raise DecompositionError(f"unit {unit.unit_id!r} must own a test file")
        if tests and not any(path in " ".join(command) for path in tests):
            raise DecompositionError(f"unit {unit.unit_id!r} verification must run its test file")
    return units


def _is_test_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("test_") or name.endswith(
        ("_test.py", ".test.js", ".test.ts", ".spec.ts")
    )


def _merge_same_file_units(units: tuple[WorkUnit, ...]) -> tuple[WorkUnit, ...]:
    """Fold units with the same owned files and the same check into the first one.

    Live codestral splits one edit of one file into a unit per changed line
    (BOD-393), each owning the same files and running the same command. That is
    one unit written several times, not a file conflict, so it is merged before
    the disjoint-ownership check. Units that share a file but differ in files or
    check still fail that check.
    """
    merged: dict[tuple[frozenset[str], tuple[str, ...]], WorkUnit] = {}
    for unit in units:
        key = (frozenset(unit.owned_files), tuple(unit.verification_command))
        first = merged.get(key)
        if first is None:
            merged[key] = unit
            continue
        merged[key] = WorkUnit(
            unit_id=first.unit_id,
            objective=f"{first.objective}; {unit.objective}",
            owned_files=first.owned_files,
            verification_command=first.verification_command,
            context="\n".join(part for part in (first.context, unit.context) if part),
        )
    return tuple(merged.values())


def _merge_paired_test_units(units: tuple[WorkUnit, ...]) -> tuple[WorkUnit, ...]:
    """Fold a unit that owns only test files into the one code unit it pairs with.

    Codestral routinely emits the code and its test as two separate units
    instead of one unit owning both files. When exactly one code unit is
    missing a test and exactly one other unit owns only test files, they are
    merged into a single unit before the "must own a test file" check runs,
    because that shape is a decomposition mechanics slip, not a content
    failure worth replanning for.
    """
    test_only = [u for u in units if u.owned_files and all(_is_test_path(p) for p in u.owned_files)]
    missing_test = [
        u
        for u in units
        if not all(_is_test_path(p) for p in u.owned_files)
        and not any(_is_test_path(p) for p in u.owned_files)
    ]
    if len(test_only) != 1 or len(missing_test) != 1:
        return units
    test_unit, code_unit = test_only[0], missing_test[0]
    merged = WorkUnit(
        unit_id=code_unit.unit_id,
        objective=code_unit.objective,
        owned_files=code_unit.owned_files + test_unit.owned_files,
        verification_command=code_unit.verification_command,
        context=code_unit.context,
    )
    return tuple(merged if u is code_unit else u for u in units if u is not test_unit)


def _attempts_message(attempts: Sequence[Mapping[str, Any]]) -> str:
    detail = "; ".join(f"{a['model']}: {a['error']}" for a in attempts)
    return f"planning failed after {len(attempts)} attempt(s): {detail}"


def _replan_evidence(evidence: str, exc: DecompositionError) -> str:
    return (
        f"{evidence}\nPrevious plan rejected: {exc}. Emit a corrected test-first plan.\n\n"
        f"{_REPLAN_EXAMPLE}"
    )


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
