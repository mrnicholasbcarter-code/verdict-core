"""Pure, offline TUI completion over a frozen local convenience snapshot.

Snapshot schema is ``verdict.completion-snapshot/v1``. Completion never
confers model health, selection, or permission to run a command.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

SCHEMA = "verdict.completion-snapshot/v1"
MAX_RESULTS = 50
MAX_TOKEN = 256
MAX_MODELS = 10_000
PAGE_SIZE = 50
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:+@-]*\Z")
_SECRET = re.compile(
    r"(?i)(?:\b(?:key|token|secret|bearer)\b(?:\s*(?:[:=]|\s)\s*[^\s;,]+)?|sk-[A-Za-z0-9_-]+)"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe(value: object, *, length: int = 100) -> str:
    """Bound and redact arbitrary display text from local data."""
    return _SECRET.sub("[redacted]", _CONTROL.sub(" ", str(value)))[:length]


def _freeze(rows: Iterable[Mapping[str, Any]], cap: int) -> tuple[Mapping[str, Any], ...]:
    def frozen_value(value: Any) -> Any:
        if isinstance(value, Mapping):
            return MappingProxyType({key: frozen_value(item) for key, item in value.items()})
        if isinstance(value, (list, tuple)):
            return tuple(frozen_value(item) for item in value)
        if isinstance(value, set):
            return frozenset(frozen_value(item) for item in value)
        return value

    from itertools import islice

    return tuple(frozen_value(row) for row in islice(rows, cap))


@dataclass(frozen=True)
class CompletionSnapshot:
    """Frozen bounded data loaded *outside* the keystroke path."""

    schema: str
    generated_at: str
    run_rows: Sequence[Mapping[str, Any]]
    model_rows: Sequence[Mapping[str, Any]]
    provider_names: Sequence[str]
    harness_targets: Sequence[str]
    modes: Sequence[str]
    source_errors: Sequence[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_rows", _freeze(self.run_rows, 200))
        object.__setattr__(self, "model_rows", _freeze(self.model_rows, MAX_MODELS))
        for name in ("provider_names", "harness_targets", "modes", "source_errors"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class CompletionCandidate:
    """Full token replacement and its negative prompt_toolkit offset."""

    text: str
    start_position: int
    display: str
    description: str | None
    kind: str


@dataclass(frozen=True)
class ArgumentSpec:
    """One field, flag, choice, model-list or run-id grammar element."""

    name: str
    kind: str
    prompted: bool
    secret: bool = False
    choices: Sequence[str] | None = None

    def __post_init__(self) -> None:
        if self.choices is not None:
            object.__setattr__(self, "choices", tuple(self.choices))


@dataclass(frozen=True)
class CommandSpec:
    """Shared command name, help syntax and argument metadata."""

    name: str
    description: str
    syntax: str
    arguments: Sequence[ArgumentSpec]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", tuple(self.arguments))


@dataclass(frozen=True)
class HelpResult:
    """Offline help for a known command."""

    command: str
    syntax: str
    description: str
    arguments: tuple[Mapping[str, str], ...]


def default_command_specs() -> tuple[CommandSpec, ...]:
    """Canonical PALETTE grammar, without importing home or its side effects."""
    descriptions = {
        "orchestrate": "goal to parallel workers and receipt",
        "supervise": "supervised run",
        "watch": "watch a run",
        "trace": "trace a run",
        "demo": "offline demo",
        "run-receipt": "verify a run receipt",
        "receipt": "inspect a routing receipt",
        "replay": "replay a session",
        "eligibility": "verified model eligibility",
        "probe": "consented model liveness",
        "detect": "detect providers",
        "models": "local model catalog",
        "catalog": "gateway catalog",
        "route": "route a task",
        "routing": "routing explorer for a run",
        "context": "context budget for a run",
        "compare": "compare routing results",
        "stats": "routing statistics",
        "suggest": "routing suggestions",
        "cost-report": "routing cost report",
        "config": "redacted configuration",
        "credentials": "manage credentials",
        "doctor": "gateway and harness health",
        "setup": "capability setup",
        "quickstart": "offline quickstart",
        "bootstrap": "Prime picker and read-only Claude compatibility",
        "help": "command syntax and limitations",
        "clear": "clear screen",
        "quit": "exit",
    }
    run = (ArgumentSpec("run_id", "run", True),)
    models = (ArgumentSpec("ids", "models", True),)
    grammar: dict[str, tuple[str, tuple[ArgumentSpec, ...]]] = {
        "eligibility": (
            "[status] [provider=..] [search=..] [page=..] [refresh]",
            (
                ArgumentSpec(
                    "status",
                    "choice",
                    False,
                    choices=(
                        "all",
                        "verified",
                        "stale",
                        "unverified",
                        "failed",
                        "unavailable",
                        "excluded",
                    ),
                ),
                ArgumentSpec("provider=", "field", False),
                ArgumentSpec("search=", "field", False),
                ArgumentSpec("page=", "field", False),
                ArgumentSpec("refresh", "flag", False),
            ),
        ),
        "bootstrap": (
            "prime [ids...] | prime restore [transaction=<id>] | claude [ids...] [mode=native|openai-side-path]",
            (
                ArgumentSpec("target", "choice", True, choices=("prime", "claude")),
                ArgumentSpec("ids", "models", False),
                ArgumentSpec("mode=", "choice", False, choices=("native", "openai-side-path")),
            ),
        ),
        "probe": ("<ids>", models),
        "credentials": (
            "[list|set <name> <value>|unset <name>]",
            (
                ArgumentSpec("name", "field", True, secret=True),
                ArgumentSpec("value", "field", True, secret=True),
            ),
        ),
        "route": ("<task> [criticality]", (ArgumentSpec("task", "field", True),)),
        "compare": ("<task> [criticality]", (ArgumentSpec("task", "field", True),)),
        "help": ("[command]", (ArgumentSpec("command", "choice", False),)),
    }
    for name in ("watch", "trace", "run-receipt", "receipt", "routing", "context", "replay"):
        grammar[name] = ("<run_id>", run)
    return tuple(
        CommandSpec(
            name,
            description,
            "/" + name + (" " + grammar[name][0] if name in grammar else ""),
            grammar[name][1] if name in grammar else (),
        )
        for name, description in descriptions.items()
    )


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _valid(snapshot: CompletionSnapshot, now: datetime) -> bool:
    generated = _stamp(snapshot.generated_at)
    return (
        snapshot.schema == SCHEMA
        and generated is not None
        and now.tzinfo is not None
        and generated <= now
        and not snapshot.source_errors
    )


def _rank(token: str, value: str, *, fuzzy: bool = False) -> int | None:
    t, v = token.casefold(), value.casefold()
    if t == v:
        return 0
    if v.startswith(t):
        return 1
    if t in v:
        return 2
    if fuzzy and t and (iter_match(t, v) or difflib.SequenceMatcher(None, t, v).ratio() >= 0.65):
        return 3
    return None


def iter_match(needle: str, haystack: str) -> bool:
    """True when needle is an ordered subsequence of haystack."""
    chars = iter(haystack)
    return all(any(char == letter for letter in chars) for char in needle)


def _bounded(limit: int) -> int:
    return min(MAX_RESULTS, max(1, limit))


def _offer(
    token: str,
    options: Iterable[tuple[str, str, str]],
    *,
    limit: int,
    fuzzy: bool = False,
    prefix: str = "",
) -> tuple[CompletionCandidate, ...]:
    scored: dict[str, tuple[int, CompletionCandidate]] = {}
    for value, description, kind in options:
        score = _rank(token, value, fuzzy=fuzzy)
        if score is None:
            continue
        text = prefix + value
        candidate = CompletionCandidate(text, -len(prefix + token), text, _safe(description), kind)
        previous = scored.get(text)
        if previous is None or score < previous[0]:
            scored[text] = (score, candidate)
    return tuple(
        entry[1]
        for _, entry in sorted(
            scored.items(), key=lambda item: (item[1][0], item[0].casefold(), item[0])
        )[: _bounded(limit)]
    )


def _model_description(row: Mapping[str, Any], value: str, now: datetime) -> str:
    status = str(row.get("status", "UNVERIFIED")).upper()
    expiry = _stamp(row.get("fresh_until"))
    checked = _stamp(row.get("checked_at"))
    if status == "VERIFIED" and checked is None:
        label = "UNVERIFIED snapshot; missing proof"
    elif status == "VERIFIED" and (expiry is None or now >= expiry):
        label = "STALE snapshot; recheck required"
    elif status == "VERIFIED":
        label = "VERIFIED snapshot (not launch authority)"
    else:
        label = f"{_safe(status)} snapshot; not verified"
    return (
        f"{label}; provider={_safe(row.get('provider', 'unknown'))}; "
        f"checked_at={_safe(row.get('checked_at', 'unknown'))}; "
        f"fresh_until={_safe(row.get('fresh_until', 'unknown'))}; "
        f"expires_at={_safe(row.get('expires_at', 'unknown'))}; "
        f"name={_safe(row.get('name', value))}"
    )


def _model_token(token: str) -> tuple[str, str]:
    """Split a comma list after the last delimiter; preserve earlier ids."""
    head, separator, tail = token.rpartition(",")
    return (head + separator, tail) if separator else ("", token)


def complete(
    text_before_cursor: str,
    *,
    commands: Sequence[CommandSpec],
    snapshot: CompletionSnapshot,
    now: datetime,
    limit: int = 20,
) -> tuple[CompletionCandidate, ...]:
    """Return bounded full-token replacements using only injected immutable facts."""
    limit = _bounded(limit)
    before, separator, token = text_before_cursor.rpartition(" ")
    if len(token) > MAX_TOKEN or len(text_before_cursor) > 4096:
        return ()
    if not separator:
        slash = token.startswith("/")
        prefix = token[1:] if slash else token
        options = _offer(
            prefix,
            ((spec.name, spec.description + "; " + spec.syntax, "command") for spec in commands),
            limit=limit,
            fuzzy=True,
            prefix="/",
        )
        return options if slash else tuple(replace(c, start_position=-len(token)) for c in options)
    words = before.split()
    if not words:
        return ()
    spec = next((s for s in commands if s.name == words[0].lstrip("/")), None)
    if spec is None:
        return ()
    args = words[1:]
    # No argument from a secret command may surface in completion.
    if any(arg.secret for arg in spec.arguments):
        return ()
    if token.startswith("-") or any(value.startswith("-") for value in args):
        return ()
    if spec.name == "eligibility":
        return _eligibility(token, args, snapshot, now, limit)
    if spec.name == "bootstrap":
        if not args:
            choices = ("prime", "claude")
            return _offer(
                token, ((v, "harness target", "choice") for v in choices), limit=limit, fuzzy=True
            )
        if args[0] not in ("prime", "claude"):
            return ()
        if args[0] == "prime":
            if len(args) == 1:
                special = _offer(
                    token,
                    (("restore", "preview safe restoration", "choice"),),
                    limit=limit,
                    fuzzy=True,
                )
            else:
                special = ()
            if "restore" in args[1:]:
                return (
                    _offer(
                        token,
                        (("transaction=", "optional restore transaction", "field"),),
                        limit=limit,
                    )
                    if len(args) == 2
                    else ()
                )
        else:
            special = _offer(token, (("mode=", "Claude report mode", "field"),), limit=limit)
            if token.startswith("mode="):
                return _offer(
                    token[5:],
                    (
                        (v, "read-only Claude report", "choice")
                        for v in ("native", "openai-side-path")
                    ),
                    prefix="mode=",
                    limit=limit,
                )
        return (special + _models(token, snapshot, now, limit))[:limit]
    for argument in spec.arguments:
        if argument.kind == "models":
            return _models(token, snapshot, now, limit)
        if argument.kind == "run":
            return _offer(
                token,
                (
                    (
                        str(row.get("run", row.get("id", ""))),
                        str(row.get("outcome", "unknown")),
                        "run",
                    )
                    for row in snapshot.run_rows
                    if row.get("run", row.get("id"))
                ),
                limit=limit,
            )
        if argument.kind == "choice" and argument.choices:
            return _offer(
                token, ((v, "choice", "choice") for v in argument.choices), limit=limit, fuzzy=True
            )
        if argument.kind == "flag":
            return _offer(token, ((argument.name, "flag", "flag"),), limit=limit)
    return ()


def _models(
    token: str, snapshot: CompletionSnapshot, now: datetime, limit: int
) -> tuple[CompletionCandidate, ...]:
    head, part = _model_token(token)
    if part.startswith("-") or len(part) > MAX_TOKEN:
        return ()
    if not _valid(snapshot, now):
        return ()
    matches = []
    for row in snapshot.model_rows:
        value = row.get("route_id", row.get("id", ""))
        if isinstance(value, str) and 0 < len(value) <= MAX_TOKEN and _SAFE_ID.fullmatch(value):
            score = _rank(part, value)
            if score is not None:
                matches.append((score, value, row))
    matches.sort(key=lambda item: (item[0], item[1].casefold(), item[1]))
    results: list[CompletionCandidate] = []
    seen: set[str] = set()
    for _, value, row in matches:
        if value in seen:
            continue
        seen.add(value)
        results.append(
            CompletionCandidate(
                head + value,
                -len(token),
                head + value,
                _safe(_model_description(row, value, now), length=320),
                "model",
            )
        )
        if len(results) == limit:
            break
    return tuple(results)


def _eligibility(
    token: str, args: list[str], snapshot: CompletionSnapshot, now: datetime, limit: int
) -> tuple[CompletionCandidate, ...]:
    statuses = (
        next(
            (
                a.choices
                for spec in default_command_specs()
                if spec.name == "eligibility"
                for a in spec.arguments
                if a.name == "status"
            ),
            (),
        )
        or ()
    )
    if token.startswith("provider="):
        return (
            _offer(
                token[9:],
                (
                    (v, "cached provider", "choice")
                    for v in snapshot.provider_names
                    if isinstance(v, str) and len(v) <= MAX_TOKEN and _SAFE_ID.fullmatch(v)
                ),
                prefix="provider=",
                limit=limit,
            )
            if _valid(snapshot, now)
            else ()
        )
    if token.startswith("search="):
        return tuple(
            replace(
                candidate,
                text="search=" + candidate.text,
                display="search=" + candidate.display,
                start_position=-len(token),
            )
            for candidate in _models(token[7:], snapshot, now, limit)
        )
    if token.startswith("page="):
        if not _valid(snapshot, now):
            return ()
        pages = max(1, (len(snapshot.model_rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        return _offer(
            token[5:],
            ((str(i), "local snapshot page", "choice") for i in range(1, pages + 1)),
            prefix="page=",
            limit=limit,
        )
    options: list[tuple[str, str, str]] = []
    if not any(arg in statuses for arg in args):
        options.extend((v, "eligibility status", "choice") for v in statuses)
    for field in ("provider=", "search=", "page=", "refresh"):
        if field not in args and not any(a.startswith(field) for a in args):
            options.append((field, "TUI flag or local filter", "flag"))
    return _offer(token, options, limit=limit, fuzzy=True)


def syntax_help(
    command: str,
    *,
    commands: Sequence[CommandSpec],
    snapshot: CompletionSnapshot,
    now: datetime | None = None,
) -> HelpResult | None:
    """Explain the shared syntax and local snapshot limits without I/O."""
    name = command.lstrip("/")
    spec = next((s for s in commands if s.name == name), None)
    if spec is None:
        return None
    generated = _stamp(snapshot.generated_at)
    snapshot_note = (
        "snapshot unavailable; run /eligibility explicitly"
        if snapshot.schema != SCHEMA
        or snapshot.source_errors
        or generated is None
        or (now is not None and generated is not None and generated > now)
        else "local snapshot only; proof may be stale; recheck with /eligibility"
    )
    details = {
        "bootstrap": "Prime interactive scope only: preview then confirm; prime restore previews reversal. "
        "Claude native/openai-side-path are read-only reports, not model selection.",
        "eligibility": "TUI uses status, provider=, search=, page=, refresh; CLI uses --flags. "
        "Refresh is explicit and may require consent.",
        "probe": "Exact comma/space-separated ids only; --probe is not an id. Consent is required.",
    }
    args = tuple(
        MappingProxyType(
            {
                "name": arg.name,
                "info": ("prompted " if arg.prompted else "optional ")
                + arg.kind
                + ("; secret, no completion" if arg.secret else "")
                + ("; " + "/".join(arg.choices) if arg.choices else ""),
            }
        )
        for arg in spec.arguments
    )
    return HelpResult(
        spec.name,
        spec.syntax,
        _safe(spec.description) + "; " + details.get(name, "") + " " + snapshot_note,
        args,
    )


def suggest_command(
    token: str, *, commands: Sequence[CommandSpec], limit: int = 3
) -> tuple[str, ...]:
    """Return full actionable slash commands for nonexecuting typo hints."""
    return tuple(
        c.text
        for c in _offer(
            token.lstrip("/"),
            ((s.name, s.description, "command") for s in commands),
            prefix="/",
            fuzzy=True,
            limit=limit,
        )
    )


class VerdictCompleter(Completer):
    """Thin prompt_toolkit adapter; caller owns snapshot loading and keybindings."""

    def __init__(
        self, commands: Sequence[CommandSpec], snapshot: CompletionSnapshot, now: datetime
    ):
        self.commands = tuple(commands)
        self.snapshot = snapshot
        self.now = now

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        """Yield replacements anchored to text before the cursor only."""
        for candidate in complete(
            document.text_before_cursor,
            commands=self.commands,
            snapshot=self.snapshot,
            now=self.now,
        ):
            yield Completion(
                candidate.text,
                start_position=candidate.start_position,
                display=candidate.display,
                display_meta=candidate.description,
            )
