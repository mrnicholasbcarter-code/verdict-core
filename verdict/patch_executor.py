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

import re
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict.probes import ProbeTransport, openai_probe_transport
from verdict.work_unit import WorkUnit, WorkUnitError, normalize_owned_path

DEFAULT_BASE_URL = "http://localhost:20128/v1"
DEFAULT_SESSION_ID = "verdict-operational-loop"

_FENCE_RE = re.compile(r"```(?:diff|patch)?\s*\n(.*?)(?:\n```|\Z)", re.DOTALL)
_DIFF_HEADER_RE = re.compile(r"^(?:---|\+\+\+)\s+(\S+)", re.MULTILINE)

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
        self._transport = transport or openai_probe_transport(
            config.base_url,
            api_key=config.api_key,
            opener=urllib.request.urlopen,
            max_response_bytes=config.max_response_bytes,
            session_id=config.session_id,
        )
        self._runner = runner
        self._observer = observer
        self._last_observation: RouteObservation | None = None

    def execute_unit(self, unit: WorkUnit) -> PatchAttempt:
        """Request a patch for ``unit`` and apply it if it stays in bounds."""
        self._last_observation = None
        started = time.monotonic()
        try:
            content, usage = self._request_patch(unit)
        except PatchExecutorError as exc:
            return self._attempt(unit, "error", str(exc), started=started)

        # From here the call succeeded, so anything wrong is the model's output:
        # a refusal to apply, not an infrastructure error.
        try:
            diff = extract_diff(content)
            paths = parse_patch_paths(diff)
        except PatchExecutorError as exc:
            return self._attempt(unit, "rejected", str(exc), usage=usage, started=started)

        outside = unit.out_of_bounds(paths)
        if outside:
            return self._attempt(
                unit,
                "rejected",
                f"patch touches files outside the unit boundary: {list(outside)}",
                usage=usage,
                started=started,
            )

        check = self._git_apply(diff, check_only=True)
        if check.returncode != 0:
            return self._attempt(
                unit,
                "rejected",
                f"git apply --check failed: {_tail(check.stderr)}",
                usage=usage,
                started=started,
            )

        applied = self._git_apply(diff, check_only=False)
        if applied.returncode != 0:
            return self._attempt(
                unit,
                "error",
                f"git apply failed after a passing check: {_tail(applied.stderr)}",
                usage=usage,
                started=started,
            )

        return self._attempt(
            unit, "applied", "", changed_files=tuple(sorted(paths)), usage=usage, started=started
        )

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

    def _request_patch(self, unit: WorkUnit) -> tuple[str, TokenUsage]:
        """Return the raw model text and its measured usage, or raise on transport failure."""
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_unit_prompt(unit, self.repo_root)},
            ],
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
        return extract_content(body), usage

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
            from dataclasses import replace

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
        target = root / path
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            sections += ["", f"--- {path} (unreadable: {type(exc).__name__}) ---"]
            continue
        sections += ["", f"--- current contents of {path} ---", text]
    sections += ["", "Reply with the unified diff only."]
    return "\n".join(sections)


_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _recount_hunks(diff: str) -> str:
    """Rewrite hunk headers with counts recomputed from actual hunk bodies.

    Models routinely emit miscounted ``@@ -a,b +c,d @@`` headers; git rejects
    those as corrupt even when every line of the change itself is correct.
    Trailing pure-context blank lines are dropped because models also add a
    phantom EOF newline context that no file contains.
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
        old = sum(1 for line in body if not line.startswith("+"))
        new = sum(1 for line in body if not line.startswith("-"))
        out.append(f"@@ -{match.group(1)},{old} +{match.group(3)},{new} @@")
        out.extend(body)
        i = j
    return "\n".join(out) + "\n"


def extract_diff(content: str) -> str:
    """Pull a unified diff out of a model response, tolerating a code fence."""
    if not isinstance(content, str) or not content.strip():
        raise PatchExecutorError("model returned an empty response")
    text = content.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not ("--- " in text and "+++ " in text) and "diff --git" not in text:
        raise PatchExecutorError("model response is not a unified diff")
    return _recount_hunks(text)


def parse_patch_paths(diff: str) -> tuple[str, ...]:
    """Return every repo-relative path named in the diff's ``---``/``+++`` headers.

    Raises if the diff names no paths or names one that cannot be normalized,
    so an unparseable header is a refusal rather than a silent bypass.
    """
    paths: set[str] = set()
    for raw in _DIFF_HEADER_RE.findall(diff):
        if raw == "/dev/null":
            continue
        candidate = raw
        if candidate.startswith(("a/", "b/")):
            candidate = candidate[2:]
        candidate = candidate.split("\t", 1)[0]
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


def load_patch_executor(
    repo_root: str | Path,
    model: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    timeout_seconds: float = 120.0,
) -> PatchExecutor:
    """Convenience constructor for CLI callers."""
    return PatchExecutor(
        repo_root,
        PatchExecutorConfig(
            model=model, base_url=base_url, api_key=api_key, timeout_seconds=timeout_seconds
        ),
    )


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
    "build_unit_prompt",
    "extract_content",
    "extract_diff",
    "load_patch_executor",
    "parse_patch_paths",
    "parse_usage",
]
