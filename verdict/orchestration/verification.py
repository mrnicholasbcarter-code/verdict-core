"""Shared verification argv resolution for worker prompts and runtime checks."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence


def resolve_verify_argv(argv: Sequence[str]) -> tuple[list[str], str]:
    """Return the executed argv and an interpreter substitution, if one was needed.

    Bare python/python3 names missing from PATH use Verdict's interpreter.
    The second return value is empty when no substitution was made; it never
    records a guessed executable path.
    """
    resolved = list(argv)
    resolved_argv0 = ""
    if argv and argv[0] in {"python", "python3"} and shutil.which(argv[0]) is None:
        resolved[0] = sys.executable
        resolved_argv0 = sys.executable
    return resolved, resolved_argv0
