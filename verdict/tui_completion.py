import difflib
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document


@dataclass(frozen=True)
class CompletionSnapshot:
    schema: str
    generated_at: str
    run_rows: list[dict[str, Any]]
    model_rows: list[dict[str, Any]]
    provider_names: list[str]
    harness_targets: list[str]
    modes: list[str]
    source_errors: list[str]


@dataclass(frozen=True)
class CompletionCandidate:
    text: str
    start_position: int
    display: str
    description: str | None
    kind: str


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    kind: str  # "field" | "flag" | "choice" | "models" | "run"
    prompted: bool
    secret: bool = False
    choices: list[str] | None = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    syntax: str
    arguments: list[ArgumentSpec]


@dataclass(frozen=True)
class HelpResult:
    command: str
    syntax: str
    description: str
    arguments: list[dict[str, str]]


def _score_candidate(token: str, candidate: str) -> float | None:
    token_lower = token.lower()
    candidate_lower = candidate.lower()
    if candidate_lower == token_lower:
        return 0.0
    if candidate_lower.startswith(token_lower):
        return 1.0
    if token_lower in candidate_lower:
        return 2.0
    ratio = difflib.SequenceMatcher(None, token_lower, candidate_lower).ratio()
    if ratio > 0.5:
        return 3.0 - ratio
    return None


def _rank_candidates(token: str, candidates: Iterable[str]) -> list[str]:
    if not token:
        return sorted(list(set(candidates)))

    scored = []
    for c in set(candidates):
        score = _score_candidate(token, c)
        if score is not None:
            scored.append((score, c))

    scored.sort(key=lambda x: (x[0], x[1]))
    return [c for _, c in scored]


def complete(
    text_before_cursor: str,
    *,
    commands: list[CommandSpec],
    snapshot: CompletionSnapshot,
    now: datetime,
    limit: int = 20,
) -> tuple[CompletionCandidate, ...]:
    start_t = time.perf_counter()

    if not text_before_cursor:
        cmd_names = _rank_candidates("", [c.name for c in commands])
        res = []
        for c_name in cmd_names:
            spec = next(s for s in commands if s.name == c_name)
            res.append(
                CompletionCandidate(
                    text=f"/{c_name}",
                    start_position=0,
                    display=f"/{c_name}",
                    description=spec.description,
                    kind="command",
                )
            )

        final = res[:limit]
        duration = (time.perf_counter() - start_t) * 1000
        assert duration < 50, f"Completion too slow: {duration:.2f}ms"
        return tuple(final)

    if text_before_cursor.startswith("/") and " " not in text_before_cursor:
        prefix = text_before_cursor[1:]
        cmd_names = _rank_candidates(prefix, [c.name for c in commands])
        res = []
        for c_name in cmd_names:
            spec = next(s for s in commands if s.name == c_name)
            full_cmd = f"/{c_name}"
            res.append(
                CompletionCandidate(
                    text=full_cmd[len(text_before_cursor) :],
                    start_position=-len(text_before_cursor),
                    display=full_cmd,
                    description=spec.description,
                    kind="command",
                )
            )

        final = res[:limit]
        duration = (time.perf_counter() - start_t) * 1000
        assert duration < 50, f"Completion too slow: {duration:.2f}ms"
        return tuple(final)

    if not text_before_cursor.startswith("/"):
        return ()

    parts = text_before_cursor.split()
    if not parts:
        return ()

    first_token = parts[0].lstrip("/")
    spec_opt = next((s for s in commands if s.name == first_token), None)
    if spec_opt is None:
        return ()

    spec = spec_opt

    last_space_idx = text_before_cursor.rfind(" ")
    if last_space_idx == -1:
        return ()

    current_token = text_before_cursor[last_space_idx + 1 :]

    prefix_tokens = text_before_cursor[:last_space_idx].split()
    arg_index = len(prefix_tokens) - 1

    if arg_index < 0 or arg_index >= len(spec.arguments):
        return ()

    arg_spec = spec.arguments[arg_index]

    if current_token.startswith("--"):
        return ()

    res = []
    if arg_spec.kind == "models":
        for row in snapshot.model_rows:
            mid = row.get("id", "")
            if not current_token or (current_token.lower() in mid.lower()):
                status = row.get("status", "UNKNOWN")
                label = ""
                if status == "STALE":
                    label = " [stale]"
                elif status == "UNVERIFIED":
                    label = " [unverified]"

                res.append(
                    CompletionCandidate(
                        text=mid[len(current_token) :],
                        start_position=last_space_idx + 1 - len(text_before_cursor),
                        display=mid,
                        description=f"{row.get('name', mid)}{label}",
                        kind="model",
                    )
                )
    elif arg_spec.kind == "run":
        for row in snapshot.run_rows:
            rid = row.get("run", "")
            if not current_token or (current_token.lower() in rid.lower()):
                res.append(
                    CompletionCandidate(
                        text=rid[len(current_token) :],
                        start_position=last_space_idx + 1 - len(text_before_cursor),
                        display=rid,
                        description=f"{row.get('outcome', 'unknown')} {row.get('age_s', 0)}s ago",
                        kind="run",
                    )
                )
    elif arg_spec.kind == "choice" and arg_spec.choices:
        for choice in arg_spec.choices:
            if not current_token or (current_token.lower() in choice.lower()):
                res.append(
                    CompletionCandidate(
                        text=choice[len(current_token) :],
                        start_position=last_space_idx + 1 - len(text_before_cursor),
                        display=choice,
                        description=None,
                        kind="choice",
                    )
                )
    elif arg_spec.kind == "flag":
        flag_name = arg_spec.name
        if not current_token or (current_token.lower() in flag_name.lower()):
            res.append(
                CompletionCandidate(
                    text=flag_name[len(current_token) :],
                    start_position=last_space_idx + 1 - len(text_before_cursor),
                    display=flag_name,
                    description=None,
                    kind="flag",
                )
            )

    final = res[:limit]
    duration = (time.perf_counter() - start_t) * 1000
    assert duration < 50, f"Completion too slow: {duration:.2f}ms"
    return tuple(final)


def syntax_help(
    command: str, *, commands: list[CommandSpec], snapshot: CompletionSnapshot
) -> HelpResult | None:
    spec = next((s for s in commands if s.name == command), None)
    if spec is None:
        return None

    args_help = []
    for arg in spec.arguments:
        info = f"<{arg.name}>" if arg.prompted else f"[{arg.name}]"
        if arg.choices:
            info += f" ({'/'.join(arg.choices)})"
        args_help.append({"name": arg.name, "info": info})

    return HelpResult(
        command=spec.name,
        syntax=f"/{spec.name} "
        + " ".join([f"<{a.name}>" if a.prompted else f"[{a.name}]" for a in spec.arguments]),
        description=spec.description,
        arguments=args_help,
    )


def suggest_command(token: str, *, commands: list[CommandSpec], limit: int = 3) -> tuple[str, ...]:
    cmds = _rank_candidates(token, [c.name for c in commands])
    return tuple(cmds[:limit])


class VerdictCompleter(Completer):
    def __init__(self, commands: list[CommandSpec], snapshot: CompletionSnapshot, now: datetime):
        self.commands = commands
        self.snapshot = snapshot
        self.now = now

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        candidates = complete(text, commands=self.commands, snapshot=self.snapshot, now=self.now)

        for cand in candidates:
            yield Completion(
                cand.text,
                start_position=cand.start_position,
                display=cand.display,
                display_meta=cand.description,
            )
