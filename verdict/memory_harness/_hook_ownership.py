"""Ownership predicate shared by the Codex and Claude Code hook installers.

Install, reinstall and uninstall only touch hook commands that Verdict wrote. A command
is Verdict-owned only when ``shlex.split`` parses it as a direct invocation of exactly
``<python> -m <module> <Event>``, with ``<Event>`` one of the harness's known events.
``<python>`` is the pinned ``sys.executable`` or any path whose basename is ``python``,
``python3`` or ``python3.N`` (absolute, relative or bare), which covers the hooks written
before the interpreter was pinned. Anything else is a user hook and is preserved: wrappers
(``sh -c '...'``, ``logger -- '...'``), extra arguments, other modules, or a command
``shlex`` cannot parse.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Collection

_PYTHON_BASENAME = re.compile(r"python(?:3(?:\.\d+)?)?(?:\.exe)?")


def _split(command: object) -> list[str] | None:
    if not isinstance(command, str):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def is_python_interpreter(token: str) -> bool:
    """True for the pinned ``sys.executable`` or a ``python``/``python3[.N]`` path."""
    if not token:
        return False
    if sys.executable and token == sys.executable:
        return True
    return _PYTHON_BASENAME.fullmatch(os.path.basename(token)) is not None


def is_module_hook(
    command: object, module: str, events: Collection[str], *, subcommand: tuple[str, ...] = ()
) -> bool:
    """True only for exactly ``[python, "-m", module, *subcommand, event]``."""
    tokens = _split(command)
    if tokens is None:
        return False
    head = 3 + len(subcommand)
    return (
        len(tokens) == head + 1
        and is_python_interpreter(tokens[0])
        and tokens[1:3] == ["-m", module]
        and tuple(tokens[3:head]) == subcommand
        and tokens[head] in events
    )


def is_legacy_tmp_prompt_hook(command: object, prompt_files: Collection[str]) -> bool:
    """True for a direct legacy ``verdict hook record|recall`` reading a Verdict prompt file.

    The pre-stdin installers wrote ``verdict hook recall '$(cat /tmp/<harness>_...)' ...``
    and ``verdict hook record '<kind>' '$(cat /tmp/<harness>_...)' ...``. The command must
    start with ``verdict hook``, and the prompt argument must be in its fixed position and
    read one of ``prompt_files``, so a wrapper around such a command stays foreign.
    """
    tokens = _split(command)
    if tokens is None or len(tokens) < 4 or tokens[:2] != ["verdict", "hook"]:
        return False
    if tokens[2] == "recall":
        prompt_arg = tokens[3]
    elif tokens[2] == "record" and len(tokens) >= 5:
        prompt_arg = tokens[4]
    else:
        return False
    return any(
        prompt_arg == f"$(cat {path})" or prompt_arg.startswith(f"$(cat {path} ")
        for path in prompt_files
    )
