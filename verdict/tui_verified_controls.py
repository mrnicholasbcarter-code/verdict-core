"""Injected controller for the verified-models TUI/CLI surfaces (BOD-292).

This module is the consumer-side glue home.py delegates to: it parses
``/eligibility`` and ``/probe`` arguments, runs the digest-bound y/N consent
prompt, and drives the wait-with-progress refresh runner. Every piece of I/O
(line/key reader, progress writer, the refresh runner, the clock) is injected
so tests drive it with no terminal, no transport and no sleeping.

home.py owns palette/dispatch/render; this controller owns ONLY the parsing,
consent and wait behaviour, so no edit to home or the registry is required.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# /eligibility argument parsing
# ---------------------------------------------------------------------------

_STATUS_VALUES = frozenset(
    {"VERIFIED", "STALE", "FAILED", "UNAVAILABLE", "UNVERIFIED", "INVENTORY_ONLY", "EXCLUDED"}
)


class ControlsError(ValueError):
    """Raised when a controller input violates the contract."""


@dataclass(frozen=True)
class EligibilityArgs:
    """Parsed ``/eligibility`` arguments. All fields are independent filters.

    ``refresh`` requests the bounded wait-with-progress refresh for the shown
    page. There is no scope prompt and no default scope.
    """

    status: str | None = None
    provider: str | None = None
    search: str | None = None
    page: int = 1
    refresh: bool = False


def parse_eligibility_args(text: str) -> EligibilityArgs:
    """Parse ``/eligibility [status|provider=|search=|page=|refresh]`` tokens.

    A bare recognised status token (case-insensitive) sets the status filter.
    ``provider=``, ``search=`` and ``page=`` take values. ``refresh`` is a flag.
    Unknown bare tokens are treated as a status only when recognised; otherwise
    they raise, so a typo never silently widens scope.
    """
    status: str | None = None
    provider: str | None = None
    search: str | None = None
    page = 1
    refresh = False
    for token in text.split():
        low = token.lower()
        if low == "refresh":
            refresh = True
        elif low.startswith("provider="):
            provider = token.split("=", 1)[1].strip() or None
        elif low.startswith("search="):
            search = token.split("=", 1)[1].strip() or None
        elif low.startswith("page="):
            raw = token.split("=", 1)[1].strip()
            try:
                page = int(raw)
            except ValueError as exc:
                raise ControlsError(f"invalid page: {raw!r}") from exc
            if page < 1:
                raise ControlsError("page must be >= 1")
        elif token.upper() in _STATUS_VALUES:
            status = token.upper()
        else:
            raise ControlsError(f"unknown /eligibility argument: {token!r}")
    return EligibilityArgs(
        status=status, provider=provider, search=search, page=page, refresh=refresh
    )


# ---------------------------------------------------------------------------
# /probe model-list parser
# ---------------------------------------------------------------------------


def parse_probe_model_list(text: str) -> list[str]:
    """Parse a ``/probe`` model list: comma and/or whitespace separated ids.

    Deduplicates while preserving first-seen order. Ids containing ``/`` or
    ``:`` are preserved exactly (``cc/x``, ``openrouter/foo:free``). An empty
    list is rejected.
    """
    raw = text.replace(",", " ")
    ids: list[str] = []
    seen: set[str] = set()
    for token in raw.split():
        token = token.strip()
        if not token:
            continue
        if token not in seen:
            seen.add(token)
            ids.append(token)
    if not ids:
        raise ControlsError("no model ids provided")
    return ids


# ---------------------------------------------------------------------------
# y/N consent (default No)
# ---------------------------------------------------------------------------

# Sentinels a reader may raise/return to signal a cancel (Esc/Ctrl-C/EOF).
CANCEL = object()


@dataclass(frozen=True)
class ConsentResult:
    """Outcome of a y/N consent prompt."""

    granted: bool
    cancelled: bool

    @property
    def denied(self) -> bool:
        return not self.granted and not self.cancelled


def prompt_consent(
    message: str, *, read_line: Callable[[str], Any], write: Callable[[str], None] | None = None
) -> ConsentResult:
    """Ask ``[y/N]`` with default No. Blank/n => denied; y/yes => granted.

    EOF, Esc or Ctrl-C cancel (``read_line`` may raise ``EOFError`` /
    ``KeyboardInterrupt`` or return the :data:`CANCEL` sentinel / ``None``).
    Cancellation and denial both make zero transport calls; the caller decides
    presentation.
    """
    prompt = f"{message} [y/N] "
    if write is not None:
        write(prompt)
    try:
        answer = read_line(prompt)
    except (EOFError, KeyboardInterrupt):
        return ConsentResult(granted=False, cancelled=True)
    if answer is CANCEL or answer is None:
        return ConsentResult(granted=False, cancelled=True)
    text = str(answer).strip().lower()
    if text in {"y", "yes"}:
        return ConsentResult(granted=True, cancelled=False)
    # Blank, "n", "no" and anything else default to No (not cancel).
    return ConsentResult(granted=False, cancelled=False)


# ---------------------------------------------------------------------------
# Wait-with-progress runner
# ---------------------------------------------------------------------------


@dataclass
class ProgressRecorder:
    """Records progress ticks in arrival order, for ordering assertions.

    The runner guarantees every progress tick is delivered BEFORE the final
    outcome is returned, so a consumer renders statuses only after completion.
    """

    events: list[Any] = field(default_factory=list)

    def __call__(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class WaitRunner:
    """Runs a bounded refresh and returns its outcome only AFTER completion.

    ``run_refresh`` is the injected coordinator call (typically a bound
    ``RefreshCoordinator.refresh_for_consumer`` / module ``refresh_for_consumer``).
    ``on_progress`` receives bounded progress ticks as they arrive. ``cancel``
    lets the consumer request shared cancellation (Esc/Ctrl-C). The runner adds
    no polling of its own: it simply forwards the injected callbacks and returns
    the coordinator's single final result, so there is never a provisional list.
    """

    run_refresh: Callable[..., Any]
    on_progress: Callable[[Any], None] | None = None
    cancel: Callable[[], bool] | None = None

    def run(
        self,
        snapshot: Any,
        *,
        consumer: str,
        needed_ids: Sequence[str],
        config: Any,
        explicit: bool = False,
        **extra: Any,
    ) -> Any:
        """Execute the refresh and return the outcome after it completes.

        Progress ticks are delivered through ``on_progress`` during the call;
        the outcome is returned only once the bounded job ends. The caller
        reloads evidence and renders strictly AFTER this returns.
        """
        return self.run_refresh(
            snapshot,
            consumer=consumer,
            needed_ids=list(needed_ids),
            config=config,
            explicit=explicit,
            on_progress=self.on_progress,
            cancel=self.cancel,
            **extra,
        )


@dataclass
class VerifiedControlsController:
    """Top-level injected controller home.py delegates to.

    All collaborators are injected: ``read_line`` reads one input line (may
    raise EOFError/KeyboardInterrupt or return :data:`CANCEL` to cancel),
    ``write`` emits presentation text (progress goes here, never to JSON
    stdout), ``run_refresh`` runs the bounded coordinator job, and
    ``is_cancelled`` reports a shared cancel request.
    """

    read_line: Callable[[str], Any]
    write: Callable[[str], None]
    run_refresh: Callable[..., Any]
    is_cancelled: Callable[[], bool] | None = None

    def eligibility(self, text: str) -> EligibilityArgs:
        return parse_eligibility_args(text)

    def probe_ids(self, text: str) -> list[str]:
        return parse_probe_model_list(text)

    def confirm(self, message: str) -> ConsentResult:
        return prompt_consent(message, read_line=self.read_line, write=self.write)

    def refresh(
        self,
        snapshot: Any,
        *,
        consumer: str,
        needed_ids: Sequence[str],
        config: Any,
        explicit: bool = False,
        on_progress: Callable[[Any], None] | None = None,
        **extra: Any,
    ) -> Any:
        """Run the bounded wait-with-progress refresh and return its outcome."""
        runner = WaitRunner(
            run_refresh=self.run_refresh,
            on_progress=on_progress or (lambda event: self.write(_format_progress(event))),
            cancel=self.is_cancelled,
        )
        return runner.run(
            snapshot,
            consumer=consumer,
            needed_ids=needed_ids,
            config=config,
            explicit=explicit,
            **extra,
        )


def _format_progress(event: Any) -> str:
    """Render one progress tick to a short presentation line (no secrets)."""
    probed = getattr(event, "probed", 0)
    total = getattr(event, "total", 0)
    verified = getattr(event, "verified", 0)
    failed = getattr(event, "failed", 0)
    requests = getattr(event, "requests_made", 0)
    return f"probing {probed}/{total} verified={verified} failed={failed} requests={requests}\n"


__all__ = [
    "CANCEL",
    "ConsentResult",
    "ControlsError",
    "EligibilityArgs",
    "ProgressRecorder",
    "VerifiedControlsController",
    "WaitRunner",
    "parse_eligibility_args",
    "parse_probe_model_list",
    "prompt_consent",
]
