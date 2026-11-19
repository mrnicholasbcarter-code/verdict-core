"""Verdict state directory resolution.

Stdlib only: low layers and the cheap ``import verdict`` path import this, so it
must not import verdict modules or third-party packages.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path


def verdict_home(env: Mapping[str, str] | None = None) -> Path:
    """``$VERDICT_HOME``, else ``$HOME/.verdict``.

    Values are stripped and a blank one counts as unset, so an empty
    ``VERDICT_HOME`` never resolves to the working directory. ``env`` replaces
    ``os.environ`` (tests); a leading ``~`` expands to that mapping's ``HOME``.
    """
    source = os.environ if env is None else env
    home = (source.get("HOME") or "").strip()
    home_dir = Path(home) if home else Path.home()
    configured = (source.get("VERDICT_HOME") or "").strip()
    if not configured:
        return home_dir / ".verdict"
    path = Path(configured)
    if path.parts[:1] == ("~",):
        return home_dir.joinpath(*path.parts[1:])
    return path.expanduser()


DECISION_LOG_NAME = "verdict-decisions.jsonl"


def default_decision_log_path(env: Mapping[str, str] | None = None) -> str:
    """Default routing decision log: ``<verdict_home()>/verdict-decisions.jsonl``.

    Never the working directory: a user running Verdict inside a repository
    must not get an untracked log file there. ``LLMGATE_LOG_PATH`` and an
    explicit ``log_path`` still override this at their call sites.
    """
    return str(verdict_home(env) / DECISION_LOG_NAME)


def resolve_decision_log_for_read(log_path: str | None) -> str:
    """Decision log a reader (stats, cost-report, suggest) should open.

    An explicit path wins, then ``LLMGATE_LOG_PATH`` (the writers' override, so
    readers open the log the writers write). Otherwise the state-directory log,
    unless only a legacy ``./verdict-decisions.jsonl`` from an older release
    exists. When both exist the state-directory log is read and one stderr
    notice names the legacy log; the two are never merged or migrated.
    """
    explicit = log_path or (os.environ.get("LLMGATE_LOG_PATH") or "").strip()
    if explicit:
        return explicit
    default = default_decision_log_path()
    legacy = Path.cwd() / DECISION_LOG_NAME
    if not legacy.is_file() or Path(default).resolve() == legacy.resolve():
        return default
    if not Path(default).exists():
        return DECISION_LOG_NAME
    _notice_hidden_legacy_log(default)
    return default


LEGACY_LOG_NOTICE = (
    "verdict: a legacy decision log ./{name} exists and is not being read; reports use {default}."
    " To read it: verdict stats --log_path ./{name}, verdict suggest --log_path ./{name},"
    " or LLMGATE_LOG_PATH=./{name} for any command (cost-report and ui have no flag)."
)
_legacy_notice_done = False


def _notice_hidden_legacy_log(default: str) -> None:
    """One stderr line per process; never stdout, which may carry one JSON document."""
    global _legacy_notice_done
    if _legacy_notice_done:
        return
    _legacy_notice_done = True
    print(LEGACY_LOG_NOTICE.format(name=DECISION_LOG_NAME, default=default), file=sys.stderr)


def scoped_state_env(root: Path) -> dict[str, str]:
    """Environment that points every state store at ``root``.

    For fixture runs (``verdict demo``): the scripted scenario must never write
    passports, health, session evidence, attempt records or the verified-run
    index into the user's state, where admission and proof would read them.
    """
    return {
        "VERDICT_HOME": str(root),
        "VERDICT_SESSION_EVIDENCE": str(root / "session-evidence.jsonl"),
        "VERDICT_HEALTH_CACHE": str(root / "health-cache.json"),
        "VERDICT_PROVE_AT_REST_STATE": str(root / "prove-at-rest" / "state.json"),
    }
