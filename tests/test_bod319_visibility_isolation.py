"""BOD-319: eligibility visibility reads honour PRIME_AGENT_* env and never
implicitly write the operator's registry.

Regression: ``prime_visibility(path=None, live_rows=...)`` hard-coded
``Path.home()/".prime"/"agent"/"models.json"`` and, whenever ``live_rows``
was supplied, unconditionally called ``refresh_omniroute_visibility(force=True)``
-- rewriting whatever registry that hard-coded path pointed at on every plain
``verdict eligibility`` / ``verdict orchestrate`` run, regardless of
PRIME_AGENT_HOME / PRIME_HOME / PRIME_AGENT_CODING_AGENT_DIR. Observed on the
operator's real ``~/.prime/agent/models.json`` on 2026-10-09 at 10:40:48Z and
13:21:30Z. Each test below fails against the pre-fix code (verified at
54d661bd) and passes after the fix (resolve_paths() env precedence +
``sync: bool = False`` default on ``prime_visibility``/``build_selector``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import NOW, REQ, conn, row
from verdict.orchestration import cli as orch_cli
from verdict.orchestration import run as orch_run
from verdict.orchestration.eligibility_report import prime_visibility
from verdict.orchestration.prime_settings import default_prime_agent_dir


def _registry(agent_dir: Path, visible: list[str]) -> Path:
    agent_dir.mkdir(parents=True, exist_ok=True)
    models = agent_dir / "models.json"
    data = {"providers": {"omniroute": {"models": [{"id": rid} for rid in visible]}}}
    models.write_text(json.dumps(data), encoding="utf-8")
    return models


def _tree(root: Path) -> set[str]:
    """All paths under ``root`` (empty set if ``root`` is absent), for a
    before/after comparison that catches ANY new file, not just one name."""
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_build_selector_honours_prime_agent_home_and_never_touches_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRIME_AGENT_HOME is read for visibility; the unrelated HOME sentinel
    dir is never created or modified, even though build_selector always
    constructs a HarnessVisibility gate."""
    home_sentinel = tmp_path / "home-sentinel"
    monkeypatch.setenv("HOME", str(home_sentinel))
    env_agent_dir = tmp_path / "env-prime" / "agent"
    monkeypatch.setenv("PRIME_AGENT_HOME", str(env_agent_dir))
    monkeypatch.delenv("PRIME_HOME", raising=False)
    monkeypatch.delenv("PRIME_AGENT_CODING_AGENT_DIR", raising=False)
    _registry(env_agent_dir, ["cc/claude-sonnet-5"])

    rows = [row("cc/claude-sonnet-5", owned_by="claude"), row("cc/claude-new-6", owned_by="claude")]
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda gw, *, api_key, timeout=30: rows)
    monkeypatch.setattr(
        orch_run, "fetch_connections", lambda gw, *, api_key, timeout=30: [conn("claude")]
    )
    monkeypatch.setattr(orch_run, "resolve_api_key", lambda *a, **k: None)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)

    ladder = orch_cli.build_selector(
        "http://127.0.0.1:1", scope="", prefer="", state_file=tmp_path / "state.json"
    )
    verdicts = {v.route_id: v for v in ladder.evaluate(REQ, now=NOW)}
    # The registry at PRIME_AGENT_HOME is the one consulted: the listed id is
    # admitted past ENTITLED, the unlisted one is refused as not visible.
    assert verdicts["cc/claude-sonnet-5"].reason != "not_harness_visible"
    assert verdicts["cc/claude-new-6"].failed_stage is not None
    assert verdicts["cc/claude-new-6"].reason == "not_harness_visible"
    assert not home_sentinel.exists()


def test_eligibility_default_never_writes_registry_even_with_live_rows(tmp_path: Path) -> None:
    """Without the --sync-visibility opt-in, prime_visibility(sync=False, the
    default) is strictly read-only: no file under the registry's directory
    is created or modified, no matter what live_rows says."""
    agent_dir = tmp_path / "agent"
    registry = _registry(agent_dir, ["kr/a"])
    before = registry.read_bytes()
    before_tree = _tree(agent_dir)

    live_rows: list[dict[str, Any]] = [{"id": "kr/a"}, {"id": "kr/new"}]
    gate = prime_visibility(registry, live_rows=live_rows)  # sync left at its default: False
    assert gate("kr/a") and not gate("kr/new")  # still reports current registry contents
    assert registry.read_bytes() == before
    assert _tree(agent_dir) == before_tree  # no backup, no sidecar, nothing


def test_sync_visibility_flag_writes_only_under_resolved_dir_with_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sync=True`` is the only path that may write, and it writes only
    under the resolved env dir (never HOME), leaving a backup of the prior
    registry bytes."""
    home_sentinel = tmp_path / "home-sentinel"
    monkeypatch.setenv("HOME", str(home_sentinel))
    env_agent_dir = tmp_path / "env-prime" / "agent"
    monkeypatch.setenv("PRIME_AGENT_HOME", str(env_agent_dir))
    monkeypatch.delenv("PRIME_HOME", raising=False)
    monkeypatch.delenv("PRIME_AGENT_CODING_AGENT_DIR", raising=False)
    registry = _registry(env_agent_dir, ["kr/a"])
    before = registry.read_bytes()

    live_rows: list[dict[str, Any]] = [{"id": "kr/a"}, {"id": "kr/new"}]
    gate = prime_visibility(live_rows=live_rows, sync=True)  # path=None: resolved from env
    assert gate("kr/new")  # the freshly-synced id is now visible
    assert registry.read_bytes() != before  # the registry itself changed
    backups = list(env_agent_dir.glob("models.json.verdict-sync-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before  # backup holds the exact pre-write bytes
    assert not home_sentinel.exists()  # still nothing under the unrelated HOME sentinel


@pytest.mark.parametrize(
    "env_setter",
    [
        lambda mp, path: mp.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(path)),
        lambda mp, path: mp.setenv("PRIME_AGENT_HOME", str(path / "agent")),
        lambda mp, path: mp.setenv("PRIME_HOME", str(path / "agent")),
        lambda mp, _path: None,  # no env at all: both fall back to Path.home()
    ],
    ids=["coding_agent_dir", "prime_agent_home", "prime_home", "fallback_home"],
)
def test_resolve_paths_agrees_with_default_prime_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_setter: Any
) -> None:
    """harness_prime.resolve_paths() and prime_settings.default_prime_agent_dir()
    must resolve to the same agent dir for each env var they both claim to
    understand (and for the common Path.home() fallback)."""
    from verdict.harness_prime import resolve_paths

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PRIME_AGENT_HOME", raising=False)
    monkeypatch.delenv("PRIME_HOME", raising=False)
    monkeypatch.delenv("PRIME_AGENT_CODING_AGENT_DIR", raising=False)
    env_dir = tmp_path / "custom-env-dir"
    env_setter(monkeypatch, env_dir)

    resolved = resolve_paths().agent_home
    expected = default_prime_agent_dir()
    if os.environ.get("PRIME_AGENT_CODING_AGENT_DIR") or not (
        os.environ.get("PRIME_AGENT_HOME") or os.environ.get("PRIME_HOME")
    ):
        # The one env var both functions read, or no env at all (common
        # Path.home() fallback): they must agree exactly.
        assert resolved == expected
    else:
        # PRIME_AGENT_HOME/PRIME_HOME are Verdict-only aliases that
        # default_prime_agent_dir() does not understand at all (it only
        # reads PRIME_AGENT_CODING_AGENT_DIR before falling back to
        # Path.home()); resolve_paths() deliberately diverges here rather
        # than silently falling back and losing the operator's intent.
        assert resolved != expected
        assert resolved == env_dir / "agent"
