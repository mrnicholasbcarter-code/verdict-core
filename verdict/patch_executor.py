"""Execute a :class:`~verdict.work_unit.WorkUnit` by applying a model-authored patch.

The model is asked for a unified diff and nothing else.  Verdict, not the model,
decides whether that diff is allowed to touch the working tree: every path in
the diff headers is checked against the unit's ``owned_files`` *before* ``git
apply`` runs, so a unit physically cannot edit a file outside its boundary.
Containment is structural rather than detected after the fact.

Token counts come from the provider's ``usage`` block.  When a response omits
it, that is recorded as unknown rather than estimated.

Every call also emits a :class:`RouteObservation` to an optional observer.  This
is the only place a *use-time* fact about a route enters Verdict (issue #272
DEF-8): a pre-flight probe shows a route that was reachable, while this shows the
route that was actually invoked.  The distinction the observation preserves is
the load-bearing one — a route that refused is evidence about the route, whereas
a model that answered badly is not.
"""

from __future__ import annotations

import ast
import difflib
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from verdict.http_safety import open_no_redirect
from verdict.probes import ProbeTransport, openai_probe_transport
from verdict.relay import fatal_identity_mismatch
from verdict.repository_files import UnsafeRepositoryPathError, read_repository_text
from verdict.work_unit import WorkUnit, WorkUnitError, normalize_owned_path

DEFAULT_BASE_URL = "http://localhost:20128/v1"
DEFAULT_SESSION_ID = "verdict-operational-loop"

_FENCE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)
_BARE_FENCE_LINE_RE = re.compile(r"^```[ \t]*\S*[ \t]*$\n?", re.MULTILINE)
_DIFF_HEADER_RE = re.compile(
    r"^(?:---|\+\+\+|rename from|rename to|copy from|copy to)[ \t]+(.+?)(?:\t.*)?$", re.MULTILINE
)

# OmniRoute 3.8.51 skips both cache read and write on either header
# (isBypassed / isCacheableForRead / isCacheableForWrite). Executor calls must
# be real samples: a replayed reply makes a retry or a repeat run meaningless.
NO_CACHE_HEADERS = {"X-OmniRoute-No-Cache": "true", "Cache-Control": "no-cache"}

# A full-file repair turn is offered only when every owned file is this small,
# so a complete rewrite stays well inside max_tokens.
FULL_FILE_REPAIR_MAX_LINES = 300

_FULL_FILE_BLOCK_RE = re.compile(r"^```file:[ \t]*(\S+)[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)

# Larger files are repaired with exact search/replace blocks instead (BOD-395).
_EDIT_FENCE_RE = re.compile(r"^```edit:[ \t]*(\S+)[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
_SEARCH_REPLACE_RE = re.compile(
    r"^<<<<<<< SEARCH\n(.*?)^=======\n(.*?)^>>>>>>> REPLACE[ \t]*(?:\n|\Z)", re.M | re.S
)

SYSTEM_PROMPT = (
    "You are a patch generator. Reply with a single unified diff and nothing else: "
    "no prose, no explanation, no code fences. Use `diff --git a/<path> b/<path>` "
    "headers with repository-relative paths. Modify ONLY the files you are told you "
    "own; a diff touching any other path is discarded unapplied. Keep the change "
    "minimal and make the stated verification command pass."
)


class PatchExecutorError(RuntimeError):
    """Raised when a patch cannot be requested, parsed, or applied."""


@dataclass(frozen=True)
class PatchExecutorConfig:
    """Connection and budget settings for one executor route.

    ``api_key`` is passed explicitly and never read from the environment or
    logged, matching :func:`verdict.probes.openai_probe_transport`.
    """

    model: str
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    timeout_seconds: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.0
    max_response_bytes: int = 1_048_576
    session_id: str = DEFAULT_SESSION_ID


@dataclass(frozen=True)
class RouteObservation:
    """A use-time fact about one route, observed on a real call.

    ``outcome`` is ``ok`` or ``route_failure``.  Only a transport failure or a
    provider-level refusal is a ``route_failure``; a model that returned an
    unusable diff answered fine and is *not* recorded as one, because admitting
    a route and liking its output are different questions.

    ``resolved_model`` is the identity the provider itself reported on the
    response (Layer-2 R5).  Comparing it against ``model`` is the only check
    that can catch a gateway silently serving a route other than the one
    selected, and it works on any OpenAI-compatible endpoint.
    """

    model: str
    outcome: str
    reason: str = ""
    status_code: int | None = None
    resolved_model: str | None = None

    @property
    def failed(self) -> bool:
        return self.outcome == "route_failure"

    @property
    def identity_mismatch(self) -> bool:
        """True when the provider answered as a model other than the one selected."""
        return self.resolved_model is not None and self.resolved_model != self.model

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "outcome": self.outcome,
            "reason": self.reason,
            "status_code": self.status_code,
            "resolved_model": self.resolved_model,
            "identity_mismatch": self.identity_mismatch,
        }


RouteObserver = Callable[[RouteObservation], None]
"""Callback invoked once per executed call with the observed route fact."""


@dataclass(frozen=True)
class TokenUsage:
    """Measured token counts, or ``reported=False`` when the provider omitted them."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reported: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reported": self.reported,
        }


@dataclass(frozen=True)
class PatchAttempt:
    """Outcome of one execution attempt against one unit.

    ``outcome`` is one of ``applied``, ``rejected``, or ``error``.  ``rejected``
    means Verdict refused the diff (out of bounds, malformed, or unapplicable)
    and the working tree was not touched.
    """

    unit_id: str
    model: str
    outcome: str
    reason: str = ""
    changed_files: tuple[str, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    resolved_model: str | None = None
    repair_attempt: dict[str, Any] | None = None

    @property
    def applied(self) -> bool:
        return self.outcome == "applied"

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "model": self.model,
            "outcome": self.outcome,
            "reason": self.reason,
            "changed_files": list(self.changed_files),
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
            "resolved_model": self.resolved_model,
            "repair_attempt": self.repair_attempt,
        }


class PatchExecutor:
    """Turn a work unit into an applied patch, or an explained refusal."""

    def __init__(
        self,
        repo_root: str | Path,
        config: PatchExecutorConfig,
        *,
        transport: ProbeTransport | None = None,
        runner: Any = subprocess.run,
        observer: RouteObserver | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        if not (self.repo_root / ".git").exists():
            raise PatchExecutorError(f"not a git repository: {self.repo_root}")
        self.config = config
        self.response_headers: dict[str, str] = {}

        def opener(request: Any, *, timeout: float) -> Any:
            response = open_no_redirect(request, timeout=timeout)
            self.response_headers = {
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower().startswith("x-omniroute-")
            }
            return response

        self._transport = transport or openai_probe_transport(
            config.base_url,
            api_key=config.api_key,
            opener=opener,
            max_response_bytes=config.max_response_bytes,
            session_id=config.session_id,
            extra_headers=NO_CACHE_HEADERS,
        )
        self._runner = runner
        self._observer = observer
        self._last_observation: RouteObservation | None = None
        self.last_repair_attempt: dict[str, Any] | None = None

    def execute_unit(self, unit: WorkUnit) -> PatchAttempt:
        """Request a patch for ``unit`` and apply it if it stays in bounds.

        When the first diff is rejected for a patch-shape reason (an
        unparseable diff, a bare-hunk mismatch, or a failed ``git apply
        --check``), the same route gets one repair turn with the exact error.
        If every owned file is small, the turn asks for complete file
        contents; if a larger owned file exists, it asks for exact
        search/replace blocks; Verdict builds the diff itself in both cases.
        A unit that owns a missing file gets a corrected-diff turn against
        the line-numbered files. Either way the result
        goes through the same boundary and apply checks. A rejection for any
        other reason (out-of-bounds files, an identity mismatch) gets no
        repair turn.
        """
        self._last_observation = None
        self.response_headers = {}
        self.last_repair_attempt = None
        started = time.monotonic()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_unit_prompt(unit, self.repo_root)},
        ]
        failure, content, usage = self._turn(unit, messages, started)
        if failure is not None:
            return failure
        attempt, repairable = self._apply_diff_reply(unit, content, usage, started)
        if not repairable:
            return attempt

        first_error = attempt.reason
        repair_format = repair_format_for(unit, self.repo_root)
        if repair_format == "full_file":
            repair_prompt = build_full_file_repair_prompt(unit, self.repo_root, first_error)
        elif repair_format == "search_replace":
            repair_prompt = build_search_replace_repair_prompt(unit, first_error)
        else:
            repair_prompt = build_repair_prompt(unit, self.repo_root, first_error)
        messages = [
            *messages,
            {"role": "assistant", "content": content},
            {"role": "user", "content": repair_prompt},
        ]
        failure, content, repair_usage = self._turn(unit, messages, started)
        if failure is not None:
            repaired = failure
        elif repair_format == "full_file":
            repaired = self._apply_full_file_reply(unit, content, repair_usage, started)
        elif repair_format == "search_replace":
            repaired = self._apply_search_replace_reply(unit, content, repair_usage, started)
        else:
            repaired, _ = self._apply_diff_reply(unit, content, repair_usage, started)
        self.last_repair_attempt = {
            "format": repair_format,
            "first_error": first_error,
            "repaired": repaired.applied,
        }
        reason = (
            ""
            if repaired.applied
            else f"patch repair failed: {repaired.reason} (first attempt: {first_error})"
        )
        return replace(
            repaired,
            reason=reason,
            usage=_add_usage(usage, repaired.usage),
            repair_attempt=self.last_repair_attempt,
        )

    def _turn(
        self, unit: WorkUnit, messages: list[dict[str, str]], started: float
    ) -> tuple[PatchAttempt | None, str, TokenUsage]:
        """Run one model turn; return a failure attempt, or ``None`` with the reply."""
        try:
            content, usage, finish_reason = self._request_patch(messages)
        except PatchExecutorError as exc:
            return self._attempt(unit, "error", str(exc), started=started), "", TokenUsage()
        if finish_reason == "length":
            reason = (
                f"truncated: the reply hit max_tokens={self.config.max_tokens} "
                "(finish_reason=length)"
            )
            return self._attempt(unit, "rejected", reason, usage=usage, started=started), "", usage
        observation = self._last_observation
        if observation is not None and fatal_identity_mismatch(
            observation.model, observation.resolved_model
        ):
            reason = (
                "provider identity mismatch: "
                f"selected {observation.model!r}, served {observation.resolved_model!r}"
            )
            return self._attempt(unit, "error", reason, usage=usage, started=started), "", usage
        return None, content, usage

    def _apply_diff_reply(
        self, unit: WorkUnit, content: str, usage: TokenUsage, started: float
    ) -> tuple[PatchAttempt, bool]:
        """Apply a model-authored diff.

        Returns the attempt and whether the rejection is a patch-shape problem
        the repair turn can address: an unparseable diff, a bare-hunk mismatch,
        or a failed ``git apply --check``. A patch that names files outside the
        unit boundary is never repaired.
        """
        # The call succeeded, so anything wrong is the model's output:
        # a refusal to apply, not an infrastructure error.
        try:
            diff = extract_diff(content)
            paths = parse_patch_paths(diff)
        except PatchExecutorError as exc:
            return self._attempt(unit, "rejected", str(exc), usage=usage, started=started), True

        outside = unit.out_of_bounds(paths)
        if outside:
            reason = f"patch touches files outside the unit boundary: {list(outside)}"
            return self._attempt(unit, "rejected", reason, usage=usage, started=started), False

        diff = repair_new_file_diff(diff, self.repo_root)
        try:
            diff = repair_bare_hunks(diff, self.repo_root)
        except (PatchExecutorError, UnsafeRepositoryPathError, OSError) as exc:
            return self._attempt(unit, "rejected", str(exc), usage=usage, started=started), True

        return self._check_and_apply(unit, diff, paths, usage, started)

    def _apply_full_file_reply(
        self, unit: WorkUnit, content: str, usage: TokenUsage, started: float
    ) -> PatchAttempt:
        """Turn complete file contents into a diff and apply it like any other."""
        try:
            files = parse_full_file_reply(content)
            outside = unit.out_of_bounds(tuple(files))
            if outside:
                raise PatchExecutorError(
                    f"full-file reply touches files outside the unit boundary: {list(outside)}"
                )
            diff = build_full_file_diff(files, self.repo_root)
        except (PatchExecutorError, UnsafeRepositoryPathError, OSError, UnicodeDecodeError) as exc:
            return self._attempt(unit, "rejected", str(exc), usage=usage, started=started)
        attempt, _ = self._check_and_apply(unit, diff, parse_patch_paths(diff), usage, started)
        return attempt

    def _apply_search_replace_reply(
        self, unit: WorkUnit, content: str, usage: TokenUsage, started: float
    ) -> PatchAttempt:
        """Apply exact search/replace blocks in memory, then gate the diff like any other."""
        try:
            blocks = parse_search_replace_reply(content)
            outside = unit.out_of_bounds(tuple(path for path, _, _ in blocks))
            if outside:
                raise PatchExecutorError(
                    f"search/replace reply touches files outside the unit boundary: {list(outside)}"
                )
            files = apply_search_replace_blocks(blocks, self.repo_root)
            diff = build_exact_diff(files, self.repo_root)
        except (PatchExecutorError, UnsafeRepositoryPathError, OSError, UnicodeDecodeError) as exc:
            return self._attempt(unit, "rejected", str(exc), usage=usage, started=started)
        attempt, _ = self._check_and_apply(unit, diff, parse_patch_paths(diff), usage, started)
        return attempt

    def _check_and_apply(
        self, unit: WorkUnit, diff: str, paths: tuple[str, ...], usage: TokenUsage, started: float
    ) -> tuple[PatchAttempt, bool]:
        check = self._git_apply(diff, check_only=True)
        if check.returncode != 0:
            reason = f"git apply --check failed: {_tail(check.stderr)}"
            return self._attempt(unit, "rejected", reason, usage=usage, started=started), True

        applied = self._git_apply(diff, check_only=False)
        if applied.returncode != 0:
            reason = f"git apply failed after a passing check: {_tail(applied.stderr)}"
            return self._attempt(unit, "error", reason, usage=usage, started=started), False

        attempt = self._attempt(
            unit, "applied", "", changed_files=tuple(sorted(paths)), usage=usage, started=started
        )
        return attempt, False

    def for_unit(self, unit: WorkUnit) -> BoundUnitExecutor:
        """Return a :class:`~verdict.dispatcher.SubagentExecutor` bound to ``unit``."""
        return BoundUnitExecutor(self, unit)

    def _attempt(
        self,
        unit: WorkUnit,
        outcome: str,
        reason: str,
        *,
        changed_files: tuple[str, ...] = (),
        usage: TokenUsage | None = None,
        started: float,
    ) -> PatchAttempt:
        observation = self._last_observation
        return PatchAttempt(
            unit_id=unit.unit_id,
            model=self.config.model,
            outcome=outcome,
            reason=reason,
            changed_files=changed_files,
            usage=usage or TokenUsage(),
            latency_ms=int((time.monotonic() - started) * 1000),
            resolved_model=None if observation is None else observation.resolved_model,
        )

    def _observe(self, observation: RouteObservation) -> None:
        """Hand one use-time route fact to the observer, if one is installed.

        Exceptions are deliberately not caught: an observer that cannot record a
        route failure is a defect the run should surface, not swallow.
        """
        self._last_observation = observation
        if self._observer is not None:
            self._observer(observation)

    def _route_failure(self, reason: str, *, status_code: int | None = None) -> PatchExecutorError:
        """Record a use-time route failure and return the error to raise."""
        self._observe(
            RouteObservation(
                model=self.config.model,
                outcome="route_failure",
                reason=reason,
                status_code=status_code,
            )
        )
        return PatchExecutorError(reason)

    def _request_patch(self, messages: list[dict[str, str]]) -> tuple[str, TokenUsage, str | None]:
        """Return the raw model text and its measured usage, or raise on transport failure."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        try:
            response = self._transport(self.config.model, payload, self.config.timeout_seconds)
        except Exception as exc:  # transport failures are an executor outcome, not a crash
            raise self._route_failure(f"transport error: {type(exc).__name__}: {exc}") from exc

        if not isinstance(response, Mapping):
            raise self._route_failure("transport returned a non-object response")
        status = response.get("status_code")
        if isinstance(status, int) and not 200 <= status < 300:
            raise self._route_failure(f"provider returned HTTP {status}", status_code=status)
        body = response.get("body")
        if not isinstance(body, Mapping):
            raise self._route_failure(
                "provider response body is not an object",
                status_code=status if isinstance(status, int) else None,
            )

        # The call reached a provider and it answered. Whatever the content turns
        # out to be, the route itself is good, so this is an ``ok`` observation
        # even when the diff is later rejected.
        resolved = body.get("model")
        self._observe(
            RouteObservation(
                model=self.config.model,
                outcome="ok",
                status_code=status if isinstance(status, int) else None,
                resolved_model=resolved if isinstance(resolved, str) else None,
            )
        )

        usage = parse_usage(body.get("usage"))
        finish_reason: str | None = None
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                fr = first.get("finish_reason")
                if isinstance(fr, str):
                    finish_reason = fr
        return extract_content(body), usage, finish_reason

    def _git_apply(self, diff: str, *, check_only: bool) -> subprocess.CompletedProcess[str]:
        # Models frequently emit hunks with miscounted context lines;
        # --recount lets git recompute counts from the actual hunk body
        # instead of rejecting the whole patch as corrupt.
        args = ["git", "apply", "--whitespace=nowarn", "--recount"]
        if check_only:
            args.append("--check")
        args.append("-")
        result = self._runner(
            args,
            cwd=str(self.repo_root),
            input=diff,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result  # type: ignore[no-any-return]


@dataclass(frozen=True)
class BoundUnitExecutor:
    """Adapts :class:`PatchExecutor` to the ``SubagentExecutor`` protocol.

    ``dispatcher.SubagentExecutor.execute`` takes a candidate and a timeout but
    no unit, so the unit is bound here and the dispatcher's selected candidate
    is honoured as the model to run.
    """

    executor: PatchExecutor
    unit: WorkUnit

    def execute(self, candidate: Any, timeout_seconds: float) -> PatchAttempt:
        model = getattr(candidate, "model", None) or getattr(candidate, "runtime_id", None)
        executor = self.executor
        if model and model != executor.config.model:
            executor = PatchExecutor(
                executor.repo_root,
                replace(executor.config, model=model, timeout_seconds=timeout_seconds),
                transport=executor._transport,
                runner=executor._runner,
                observer=executor._observer,
            )
        return executor.execute_unit(self.unit)


def build_unit_prompt(unit: WorkUnit, repo_root: str | Path) -> str:
    """Render the user message for ``unit``, inlining the files it owns."""
    root = Path(repo_root)
    sections = [
        f"Objective: {unit.objective}",
        "",
        "Files you own (you may modify ONLY these):",
        *(f"  - {path}" for path in unit.owned_files),
        "",
        "Your change must make this command exit zero:",
        f"  {' '.join(unit.verification_command)}",
    ]
    if unit.context:
        sections += ["", "Additional context:", unit.context]
    for path in unit.owned_files:
        try:
            text = read_repository_text(root, path)
        except UnsafeRepositoryPathError as exc:
            raise PatchExecutorError(str(exc)) from exc
        except (OSError, UnicodeDecodeError) as exc:
            sections += ["", f"--- {path} (unreadable: {type(exc).__name__}) ---"]
            continue
        sections += ["", f"--- current contents of {path} ---", text]
    sections += ["", "Reply with the unified diff only."]
    return "\n".join(sections)


def build_repair_prompt(unit: WorkUnit, repo_root: str | Path, error: str) -> str:
    """Render the follow-up turn after ``git apply --check`` refused a diff.

    Line numbers let the model write hunk headers and context that match the
    file exactly; the git error says which hunk did not apply.
    """
    root = Path(repo_root)
    sections = [
        "Your diff was rejected and nothing was changed.",
        f"Exact error: {error}",
        "",
        "Write a corrected unified diff against the files exactly as shown below.",
        "Line numbers are for reference only; they are not part of the file.",
        "Context and removed lines must match the file byte for byte.",
    ]
    for path in unit.owned_files:
        try:
            text = read_repository_text(root, path)
        except UnsafeRepositoryPathError as exc:
            raise PatchExecutorError(str(exc)) from exc
        except (OSError, UnicodeDecodeError) as exc:
            sections += ["", f"--- {path} (unreadable: {type(exc).__name__}) ---"]
            continue
        numbered = [f"{n:>4}| {line}" for n, line in enumerate(text.splitlines(), start=1)]
        sections += ["", f"--- current contents of {path} (with line numbers) ---", *numbered]
    sections += ["", "Reply with the unified diff only."]
    return "\n".join(sections)


def full_file_repair_eligible(unit: WorkUnit, repo_root: str | Path) -> bool:
    """True when every owned file exists and has at most ``FULL_FILE_REPAIR_MAX_LINES`` lines."""
    for path in unit.owned_files:
        try:
            text = read_repository_text(Path(repo_root), path)
        except (UnsafeRepositoryPathError, OSError, UnicodeDecodeError):
            return False
        if len(text.splitlines()) > FULL_FILE_REPAIR_MAX_LINES:
            return False
    return True


def repair_format_for(unit: WorkUnit, repo_root: str | Path) -> str:
    """Pick the repair format: full file when small, search/replace when the files exist.

    Search/replace cannot create a file, so a unit that owns a missing file
    keeps the diff repair.
    """
    if full_file_repair_eligible(unit, repo_root):
        return "full_file"
    for path in unit.owned_files:
        try:
            read_repository_text(Path(repo_root), path)
        except (UnsafeRepositoryPathError, OSError, UnicodeDecodeError):
            return "diff"
    return "search_replace"


def build_search_replace_repair_prompt(unit: WorkUnit, error: str) -> str:
    """Ask for exact search/replace blocks against the files shown in the first message."""
    return "\n".join(
        [
            "Your diff was rejected and nothing was changed.",
            f"Exact error: {error}",
            "",
            "Do NOT send a diff this time. Reply with search/replace blocks against the",
            "files exactly as shown in the first message, in exactly this format:",
            "```edit:<path>",
            "<<<<<<< SEARCH",
            "<exact current lines, copied byte for byte>",
            "=======",
            "<the lines that replace them>",
            ">>>>>>> REPLACE",
            "```",
            "A fence may hold several blocks. Each SEARCH must match exactly once in",
            "that file: include just enough lines to be unique and do not repeat",
            "unchanged code. No prose. Files you own: " + ", ".join(unit.owned_files),
        ]
    )


def parse_search_replace_reply(content: str) -> list[tuple[str, str, str]]:
    """Return ``(path, search, replace)`` blocks, in reply order, from strict ```edit fences."""
    fences = _EDIT_FENCE_RE.findall(content or "")
    if not fences:
        raise PatchExecutorError("search/replace reply contains no ```edit:<path> block")
    blocks: list[tuple[str, str, str]] = []
    for raw_path, body in fences:
        try:
            path = normalize_owned_path(raw_path)
        except WorkUnitError as exc:
            raise PatchExecutorError(f"unusable path in search/replace block {raw_path!r}") from exc
        found = _SEARCH_REPLACE_RE.findall(body)
        if not found or _SEARCH_REPLACE_RE.sub("", body).strip():
            raise PatchExecutorError(f"malformed search/replace block for {path}")
        blocks += [(path, search, replacement) for search, replacement in found]
    return blocks


def apply_search_replace_blocks(
    blocks: list[tuple[str, str, str]], repo_root: str | Path
) -> dict[str, str]:
    """Apply blocks in order to in-memory copies; each SEARCH must match exactly once."""
    root = Path(repo_root)
    files: dict[str, str] = {}
    for number, (path, search, replacement) in enumerate(blocks, start=1):
        if path not in files:
            files[path] = read_repository_text(root, path)
        count = files[path].count(search) if search else 0
        if count != 1:
            raise PatchExecutorError(
                f"search/replace block {number} for {path} matches {count} times "
                "(it must match exactly once)"
            )
        files[path] = files[path].replace(search, replacement, 1)
    return files


def build_full_file_repair_prompt(unit: WorkUnit, repo_root: str | Path, error: str) -> str:
    """Ask for complete new file contents instead of a second diff."""
    root = Path(repo_root)
    sections = [
        "Your diff was rejected and nothing was changed.",
        f"Exact error: {error}",
        "",
        "Do NOT send a diff this time. Reply with the COMPLETE new content of each",
        "file you change, every line included, each in exactly this format:",
        "```file:<path>",
        "<entire new file content>",
        "```",
        "No prose. Files you do not include stay unchanged.",
    ]
    for path in unit.owned_files:
        sections += ["", "Current contents:", f"```file:{path}", read_repository_text(root, path)]
        sections[-1] = sections[-1].removesuffix("\n")
        sections.append("```")
    return "\n".join(sections)


def parse_full_file_reply(content: str) -> dict[str, str]:
    """Return ``{path: new_text}`` from strict ```file:<path> fenced blocks."""
    blocks = _FULL_FILE_BLOCK_RE.findall(content or "")
    if not blocks:
        raise PatchExecutorError("full-file reply contains no ```file:<path> block")
    files: dict[str, str] = {}
    for raw_path, body in blocks:
        try:
            path = normalize_owned_path(raw_path)
        except WorkUnitError as exc:
            raise PatchExecutorError(f"unusable path in full-file block {raw_path!r}") from exc
        if path in files:
            raise PatchExecutorError(f"full-file reply repeats {path}")
        files[path] = body
    return files


def build_exact_diff(files: Mapping[str, str], repo_root: str | Path) -> str:
    """Build a git diff from complete new contents, byte for byte, no normalization.

    Used for search/replace results: the in-memory text is already exact, so
    no fence-newline guess and no truncation heuristic apply.
    """
    root = Path(repo_root)
    sections: list[str] = []
    for path, text in sorted(files.items()):
        original = read_repository_text(root, path)
        if text == original:
            continue
        old_lines = original.splitlines(keepends=True)
        new_lines = text.splitlines(keepends=True)
        hunks = list(difflib.unified_diff(old_lines, new_lines, f"a/{path}", f"b/{path}"))
        sections.append(f"diff --git a/{path} b/{path}\n")
        for line in hunks:
            sections.append(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            )
    if not sections:
        raise PatchExecutorError("search/replace reply changes nothing")
    return "".join(sections)


def build_full_file_diff(files: Mapping[str, str], repo_root: str | Path) -> str:
    """Build a git diff from complete new contents, keeping the original bytes.

    The fence's closing newline is not file content, so the original file's
    final-newline state decides it. A reply that keeps fewer than half the
    lines of a file of 10+ lines, or empties a non-empty file, is treated as
    truncated and refused by name.
    """
    root = Path(repo_root)
    normalized: dict[str, str] = {}
    for path, body in files.items():
        original = read_repository_text(root, path)
        text = body.removesuffix("\n")
        if original.endswith("\n") and text:
            text += "\n"
        old_lines = original.splitlines(keepends=True)
        new_lines = text.splitlines(keepends=True)
        if (original and not text) or (
            len(old_lines) >= 10 and len(new_lines) * 2 < len(old_lines)
        ):
            raise PatchExecutorError(
                f"truncated full-file reply for {path}: {len(new_lines)} of {len(old_lines)} lines"
            )
        normalized[path] = text
    try:
        return build_exact_diff(normalized, root)
    except PatchExecutorError as exc:
        raise PatchExecutorError("full-file reply changes nothing") from exc


def _add_usage(first: TokenUsage, second: TokenUsage) -> TokenUsage:
    """Sum two turns' usage; the total is reported only if both turns reported it."""
    return TokenUsage(
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
        reported=first.reported and second.reported,
    )


_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _repair_missing_hunk_prefixes(body: list[str]) -> list[str]:
    """Add the missing leading ``' '`` to a hunk body line with no diff prefix.

    nemotron sometimes drops the required leading space on an unchanged
    context line (e.g. emits ``def f():`` instead of `` def f():``). Such a
    line is treated as context, the cheapest and only lossless reading: a
    line with no prefix was never marked ``+`` or ``-``, so the model did not
    intend to change it. If that reading is wrong, ``git apply`` still
    rejects the patch on a context mismatch; this only fixes the line's
    syntax, never its content.
    """
    return [line if line[:1] in ("+", "-", " ") else f" {line}" for line in body]


def _recount_hunks(diff: str) -> str:
    """Rewrite hunk headers with counts recomputed from actual hunk bodies.

    Models routinely emit miscounted ``@@ -a,b +c,d @@`` headers; git rejects
    those as corrupt even when every line of the change itself is correct.
    Trailing pure-context blank lines are dropped because models also add a
    phantom EOF newline context that no file contains. A body line missing
    its ``' '``/``'-'``/``'+'`` prefix is repaired as context before counting.
    """
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = _HUNK_RE.match(lines[i])
        if match is None:
            out.append(lines[i])
            i += 1
            continue
        body: list[str] = []
        j = i + 1
        while (
            j < len(lines)
            and _HUNK_RE.match(lines[j]) is None
            and not lines[j].startswith(("--- ", "+++ ", "diff --git "))
        ):
            body.append(lines[j])
            j += 1
        while body and body[-1].strip() == "":
            body.pop()
        body = _repair_missing_hunk_prefixes(body)
        old = sum(1 for line in body if not line.startswith("+"))
        new = sum(1 for line in body if not line.startswith("-"))
        out.append(f"@@ -{match.group(1)},{old} +{match.group(3)},{new} @@")
        out.extend(body)
        i = j
    return "\n".join(out) + "\n"


_DIFF_SECTION_RE = re.compile(r"(?m)^--- (.+)\n\+\+\+ (.+)\n")


def _diff_section_path(header_field: str) -> str | None:
    """Return the repo-relative path a ``---``/``+++`` header names, or ``None`` for /dev/null."""
    candidate = header_field.strip()
    if candidate.startswith('"') and candidate.endswith('"'):
        try:
            candidate = ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            return None
    candidate = candidate.split("\t", 1)[0]
    if candidate == "/dev/null":
        return None
    if candidate.startswith(("a/", "b/")):
        candidate = candidate[2:]
    return candidate


def repair_new_file_diff(diff: str, repo_root: Path) -> str:
    """Rewrite a diff section for a non-existent file into a clean new-file add.

    Models sometimes emit a modify-style diff (an ``a/`` source header, removed
    lines, or a non-``/dev/null`` old path) for a file that does not exist in
    the repository yet. Git refuses that as "depends on old contents" or "bad
    git-diff" because a modify has nothing to modify. When the diff's own
    destination path is absent from the repository, the section is rebuilt as
    a plain new-file hunk: every ``-`` line is dropped, every other line's
    content becomes an addition, and the header points at ``/dev/null``. A
    diff for a file that does exist is left untouched.
    """
    matches = list(_DIFF_SECTION_RE.finditer(diff))
    if not matches:
        return diff
    out: list[str] = []
    pos = 0
    for index, match in enumerate(matches):
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(diff)
        out.append(diff[pos : match.start()])
        source = _diff_section_path(match.group(1))
        dest = _diff_section_path(match.group(2))
        path = dest or source
        if source is None or path is None or (repo_root / path).exists():
            out.append(diff[match.start() : section_end])
        else:
            body = diff[match.end() : section_end].split("\n")
            if body and body[-1] == "":
                body.pop()
            added = [
                line[1:] if line[:1] in ("+", " ") else line
                for line in body
                if not line.startswith("-") and _HUNK_RE.match(line) is None
            ]
            header = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(added)} @@\n"
            out.append(header + "".join(f"+{line}\n" for line in added))
        pos = section_end
    out.append(diff[pos:])
    return "".join(out)


def repair_bare_hunks(diff: str, repo_root: Path) -> str:
    """Locate an unnumbered hunk by its unique, exact old source lines."""
    lines = diff.splitlines(keepends=True)
    path: str | None = None
    offset = 0
    for index, line in enumerate(lines):
        if line.startswith("--- "):
            path = None
            offset = 0
        if line.startswith("--- a/"):
            path = normalize_owned_path(line[6:].strip())
        if line.startswith("+++ b/"):
            path = normalize_owned_path(line[6:].strip())
        if line.strip() != "@@":
            continue
        if path is None:
            raise PatchExecutorError("bare hunk requires an existing repository file")
        end = index + 1
        while end < len(lines) and not lines[end].startswith(("@@", "diff --git ", "--- ")):
            end += 1
        body = lines[index + 1 : end]
        if not (repo_root / path).exists():
            if not body or any(not item.startswith(("+", " ")) for item in body):
                raise PatchExecutorError(
                    "bare new-file hunk must contain only additions or context"
                )
            lines[index] = f"@@ -0,0 +1,{len(body)} @@\n"
            lines[index + 1 : end] = ["+" + item[1:] for item in body]
            continue
        old = "".join(item[1:] for item in body if item.startswith((" ", "-")))
        source = read_repository_text(repo_root, path)
        if not old or source.count(old) != 1:
            raise PatchExecutorError("bare hunk old lines must match source exactly once")
        start = source[: source.index(old)].count("\n") + 1
        old_count = sum(item.startswith((" ", "-")) for item in body)
        new_count = sum(item.startswith((" ", "+")) for item in body)
        lines[index] = f"@@ -{start},{old_count} +{start + offset},{new_count} @@\n"
        offset += new_count - old_count
    return "".join(lines)


def _looks_like_diff(text: str) -> bool:
    return ("--- " in text and "+++ " in text) or "diff --git" in text


def extract_diff(content: str) -> str:
    """Pull a unified diff out of a model response, tolerating Markdown fences.

    A response may wrap its diff in a ```diff``` / ```patch``` fence, with
    prose before or after it; that single fenced block is unwrapped. Several
    fenced blocks that each look like a diff are ambiguous and refused rather
    than guessed at. A lone, unmatched fence line (the model closed a fence it
    never opened, or vice versa) is stripped so the diff beneath it still
    parses; this is what produced git apply's "corrupt patch" on an otherwise
    valid diff.
    """
    if not isinstance(content, str) or not content.strip():
        raise PatchExecutorError("model returned an empty response")
    text = content.strip()
    blocks = [block.strip() for block in _FENCE_BLOCK_RE.findall(text)]
    diff_blocks = [block for block in blocks if _looks_like_diff(block)]
    if len(diff_blocks) > 1:
        raise PatchExecutorError("model response contains multiple conflicting diff blocks")
    text = diff_blocks[0] if diff_blocks else _BARE_FENCE_LINE_RE.sub("", text).strip()
    if not _looks_like_diff(text):
        raise PatchExecutorError("model response is not a unified diff")
    return _recount_hunks(text)


def parse_patch_paths(diff: str) -> tuple[str, ...]:
    """Return every repo-relative path named in the diff's ``---``/``+++`` headers.

    Raises if the diff names no paths or names one that cannot be normalized,
    so an unparseable header is a refusal rather than a silent bypass.
    """
    paths: set[str] = set()
    for raw in _DIFF_HEADER_RE.findall(diff):
        # Missing files are described with this banner in the prompt. Keep the
        # path in the boundary check even when the model echoes the banner.
        candidate = raw.removesuffix(" (unreadable: FileNotFoundError) ---")
        if candidate.startswith('"') and candidate.endswith('"'):
            try:
                decoded = ast.literal_eval(candidate)
            except (SyntaxError, ValueError) as exc:
                raise PatchExecutorError(f"unusable quoted path in diff header {raw!r}") from exc
            if not isinstance(decoded, str):
                raise PatchExecutorError(f"unusable quoted path in diff header {raw!r}")
            candidate = decoded
        else:
            candidate = candidate.split("\t", 1)[0]
        if candidate == "/dev/null":
            continue
        if candidate.startswith(("a/", "b/")):
            candidate = candidate[2:]
        try:
            paths.add(normalize_owned_path(candidate))
        except WorkUnitError as exc:
            raise PatchExecutorError(f"unusable path in diff header {raw!r}: {exc}") from exc
    if not paths:
        raise PatchExecutorError("diff names no files")
    return tuple(sorted(paths))


def parse_usage(value: Any) -> TokenUsage:
    """Return measured counts from an OpenAI-style ``usage`` block, or unreported."""
    if not isinstance(value, Mapping):
        return TokenUsage()
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return TokenUsage()
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, reported=True)


def extract_content(body: Mapping[str, Any]) -> str:
    """Return the assistant text from an OpenAI-style response body."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PatchExecutorError("provider response contains no choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise PatchExecutorError("provider choice is not an object")
    message = first.get("message")
    text = _message_text(message if isinstance(message, Mapping) else None, first)
    if text:
        return text
    raise PatchExecutorError("provider choice contains no text content")


def _message_text(message: Mapping[str, Any] | None, choice: Mapping[str, Any]) -> str:
    if message is not None:
        joined = _join_content(message.get("content"))
        if joined:
            return joined
        for key in ("reasoning", "reasoning_content"):
            joined = _join_content(message.get(key))
            if joined:
                return joined
    if isinstance(choice.get("text"), str) and choice["text"]:
        return str(choice["text"])
    return ""


def _join_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item:
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "".join(parts)
    return ""


def _tail(text: str, limit: int = 400) -> str:
    cleaned = (text or "").strip()
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_SESSION_ID",
    "SYSTEM_PROMPT",
    "BoundUnitExecutor",
    "PatchAttempt",
    "PatchExecutor",
    "PatchExecutorConfig",
    "PatchExecutorError",
    "RouteObservation",
    "RouteObserver",
    "TokenUsage",
    "apply_search_replace_blocks",
    "build_exact_diff",
    "build_full_file_diff",
    "build_full_file_repair_prompt",
    "build_repair_prompt",
    "build_search_replace_repair_prompt",
    "build_unit_prompt",
    "extract_content",
    "extract_diff",
    "full_file_repair_eligible",
    "parse_full_file_reply",
    "parse_patch_paths",
    "parse_search_replace_reply",
    "parse_usage",
    "repair_format_for",
]
