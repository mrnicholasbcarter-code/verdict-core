"""No test can write the operator's real Verdict home (issue #1306).

Three layers are pinned here:

* resolution: under the per-test isolation every path ``verdict.paths`` (and the
  other state resolvers) derives lands under the per-test home, and every path
  env var ``verdict/`` reads is cleared or accounted for;
* enforcement: ``tests/helpers/home_guard.py`` refuses and reports the in-process
  writes to the real state roots it can see; its docstring section "Guarantees and
  limits" says which those are and which it cannot see. It is proved against a FAKE
  "real home" (never the operator's), including end to end in a child pytest run with
  a deliberately leaking test;
* detection: ``tests/helpers/home_snapshot.py`` snapshots a throwaway home so a
  run can list exactly which tests wrote into it.
"""

from __future__ import annotations

import contextlib
import errno
import inspect
import json
import os
import posix
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import verdict
import verdict.paths as paths_module
from tests.helpers import home_guard
from tests.helpers.home_guard import GUARD, HomeWriteGuard
from tests.helpers.home_snapshot import diff, snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _per_test_home() -> Path:
    return Path(os.environ["HOME"]).resolve()


def _under(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def test_home_is_a_temp_directory_never_the_real_home() -> None:
    home = _per_test_home()
    assert str(home) not in home_guard.REAL_HOMES
    for protected in GUARD.roots:
        assert not _under(home, Path(protected)), f"HOME {home} is inside {protected}"


# Every public function in verdict/paths.py is either a resolver exercised below
# or listed here with the reason it is not one. A new function fails this test
# until it is classified, so a new path resolver cannot dodge the check.
_PATHS_RESOLVERS = {
    "verdict_home": lambda: paths_module.verdict_home(),
    "default_decision_log_path": lambda: Path(paths_module.default_decision_log_path()),
    "resolve_decision_log_for_read": lambda: Path(paths_module.resolve_decision_log_for_read(None)),
}
_PATHS_NOT_RESOLVERS = {
    "scoped_state_env": "takes an explicit root and returns env for fixture runs (tested below)"
}


def test_every_function_in_verdict_paths_is_classified() -> None:
    public = {
        name
        for name, fn in inspect.getmembers(paths_module, inspect.isfunction)
        if fn.__module__ == paths_module.__name__ and not name.startswith("_")
    }
    unclassified = public - set(_PATHS_RESOLVERS) - set(_PATHS_NOT_RESOLVERS)
    assert not unclassified, (
        f"verdict/paths.py gained {sorted(unclassified)}: add each to _PATHS_RESOLVERS "
        "(it must resolve under the per-test home) or _PATHS_NOT_RESOLVERS with a reason"
    )


@pytest.mark.parametrize("name", sorted(_PATHS_RESOLVERS))
def test_verdict_paths_resolve_under_the_per_test_home(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # resolve_decision_log_for_read looks for a legacy log in cwd
    resolved = _PATHS_RESOLVERS[name]()
    assert _under(resolved, _per_test_home()), f"{name}() -> {resolved}"


def test_scoped_state_env_points_every_store_at_its_root(tmp_path: Path) -> None:
    env = paths_module.scoped_state_env(tmp_path)
    assert env
    for key, value in env.items():
        assert _under(Path(value), tmp_path), f"{key}={value}"


_STATE_RESOLVERS = (
    ("verdict.prove_at_rest_legacy", "default_state_path"),
    ("verdict.catalog_cache", "default_catalog_cache_path"),
    ("verdict.runtime_contract", "default_runtime_state_dir"),
    ("verdict.openrouter_free_set", "default_cache_path"),
    ("verdict.orchestration.health_cache", "default_cache_path"),
)


@pytest.mark.parametrize(("module", "function"), _STATE_RESOLVERS)
def test_state_resolvers_land_under_the_per_test_home(module: str, function: str) -> None:
    import importlib

    resolved = Path(getattr(importlib.import_module(module), function)())
    assert _under(resolved, _per_test_home()), f"{module}.{function}() -> {resolved}"


def test_session_ledger_lands_under_the_per_test_home() -> None:
    from verdict.orchestration.session_evidence import SessionLedger

    assert _under(Path(SessionLedger().path), _per_test_home())


# Env names verdict/ reads that look like a location. Each is either cleared by
# tests/conftest.py (home_guard.HOST_STATE_ENV) or explained here.
_PATH_TOKENS = frozenset(
    {"HOME", "PATH", "DIR", "DB", "FILE", "ROOT", "ROOTS", "CACHE", "STATE", "SNAPSHOT"}
    | {"EVIDENCE", "LOG", "STORE", "LEDGER", "DATA", "CONFIG", "OUTBOX"}
)
_ENV_NAME = re.compile(
    r"""["']((?:VERDICT|LLMGATE|XDG|PRIME|CODEX|CLAUDE|HERMES|OPENCODE|CLINE|ORCA)[A-Z0-9_]*)["']"""
)
_EXEMPT_PATH_ENV = {
    "VERDICT_PIPELINE_STATE": "a boolean flag that happens to end in STATE, not a location",
    "VERDICT_REQUIRE_EXECUTION_PATH": "a boolean switch (PATH here means execution path)",
    "VERDICT_RECEIPTS_DB": "set to ':memory:' by _isolate_verdict_env",
    "VERDICT_SUPERVISED_RUN_DIR": "deleted by _isolate_verdict_env",
    "VERDICT_CONTROLLER_CGROUP_ROOT": "deleted by _isolate_verdict_env",
    "CODEX_HOME": "deleted by _isolate_verdict_env",
    "PRIME_AGENT_HOME": "deleted by _isolate_home",
    "PRIME_HOME": "deleted by _isolate_home",
    "PRIME_AGENT_CODING_AGENT_DIR": "deleted by _isolate_home",
}


def _path_like_env_names_read_by_verdict() -> set[str]:
    package = Path(verdict.__file__).resolve().parent
    names: set[str] = set()
    for source in package.rglob("*.py"):
        if "openspec_vendor" in source.parts:
            continue
        for name in _ENV_NAME.findall(source.read_text(encoding="utf-8", errors="ignore")):
            if name.split("_")[-1] in _PATH_TOKENS:
                names.add(name)
    return names


# Selectors of the operator's LIVE installs that the name tripwire above cannot see (their
# last token is BIN or URL, not a location word). An export would point a test at the real
# opencode binary or the live ``verdict serve``; they are cleared like VERDICT_GATEWAY.
_LIVE_INSTALL_SELECTORS = ("VERDICT_OPENCODE_BIN", "VERDICT_SERVE_URL")


@pytest.mark.parametrize("name", _LIVE_INSTALL_SELECTORS)
def test_live_install_selectors_verdict_reads_are_cleared(name: str) -> None:
    package = Path(verdict.__file__).resolve().parent
    read = {
        found
        for source in package.rglob("*.py")
        if "openspec_vendor" not in source.parts
        for found in _ENV_NAME.findall(source.read_text(encoding="utf-8", errors="ignore"))
    }
    assert name in read, f"verdict/ no longer reads {name}: drop it from _LIVE_INSTALL_SELECTORS"
    assert name in home_guard.HOST_STATE_ENV, f"{name} is read by verdict/ but not cleared"


def test_every_path_env_var_verdict_reads_is_cleared_or_explained() -> None:
    names = _path_like_env_names_read_by_verdict()
    cleared = set(home_guard.HOST_STATE_ENV)
    unaccounted = sorted(names - cleared - set(_EXEMPT_PATH_ENV))
    assert not unaccounted, (
        f"verdict/ reads {unaccounted}: an operator export would redirect a test at real "
        "state. Add each to HOST_STATE_ENV in tests/helpers/home_guard.py, or to "
        "_EXEMPT_PATH_ENV here with the reason it is not a location."
    )
    stale = sorted(set(_EXEMPT_PATH_ENV) - names)
    assert not stale, f"_EXEMPT_PATH_ENV names that verdict/ no longer reads: {stale}"


# ---------------------------------------------------------------------------
# Enforcement: the guard against a FAKE real home
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_real_home(tmp_path: Path) -> Path:
    """A stand-in for the operator's home. The guard is told to protect it."""
    home = tmp_path / "fake-real-home"
    (home / ".verdict").mkdir(parents=True)
    (home / ".verdict" / "model-metadata.json").write_text('{"ratings": 1}')
    return home


def _flags(*names: str) -> int:
    value = 0
    for name in names:
        value |= getattr(os, name)
    return value


@pytest.mark.parametrize(
    ("event", "args", "denied"),
    [
        ("open", ("{r}/state.json", "w", _flags("O_WRONLY", "O_CREAT", "O_TRUNC")), True),
        ("open", ("{r}/log", "ab", _flags("O_WRONLY", "O_APPEND", "O_CREAT")), True),
        ("open", ("{r}/db", "r+", _flags("O_RDWR")), True),
        ("open", ("{r}/new", None, _flags("O_WRONLY", "O_CREAT", "O_EXCL")), True),
        ("open", ("{r}/state.json", "r", _flags("O_RDONLY")), False),
        ("open", ("{r}/state.json", "rb", 0), False),
        ("open", (3, "w", _flags("O_WRONLY")), False),  # an fd, not a path
        ("open", ("{o}/state.json", "w", _flags("O_WRONLY", "O_CREAT")), False),
        ("open", ("{r}-sibling/state.json", "w", _flags("O_WRONLY", "O_CREAT")), False),
        ("open", ("{r}/../escape.json", "w", _flags("O_WRONLY", "O_CREAT")), False),
        ("open", ("/{r}/state.json", "w", _flags("O_WRONLY", "O_CREAT")), True),  # //p == /p
        ("open", ("///{r}/state.json", "w", _flags("O_WRONLY", "O_CREAT")), True),
        ("open", ("{r}//sub///state.json", "w", _flags("O_WRONLY", "O_CREAT")), True),
        ("open", ("{r}/./sub/../state.json", "w", _flags("O_WRONLY", "O_CREAT")), True),
        ("open", ("{o}/../{rn}/x.json", "w", _flags("O_WRONLY", "O_CREAT")), True),
        ("os.rename", ("{o}/a", "{r}/b", None, None), True),
        ("os.rename", ("{r}/a", "{o}/b", None, None), True),
        ("os.rename", ("{o}/a", "{o}/b", None, None), False),
        ("os.rename", ("{rp}", "{o}/moved", None, None), True),  # an ANCESTOR of the root
        ("os.rename", ("{rp}/..", "{o}/moved", None, None), True),
        ("os.rename", ("{rp}/.bashrc", "{rp}/.bashrc.bak", None, None), False),  # unrelated sibling
        ("os.rename", ("{o}/tree", "{rp}", None, None), True),  # populates the root's parent
        ("shutil.move", ("{o}/tree", "{rp}"), True),
        ("shutil.move", ("{rp}", "{o}/moved"), True),
        ("shutil.rmtree", ("{rp}", None), True),
        ("shutil.rmtree", ("{rp}/other-tool", None), False),
        ("shutil.copytree", ("{rp}", "{o}/copy"), False),  # a copy only reads the source
        ("os.remove", ("{r}/model-metadata.json", None), True),
        ("os.rmdir", ("{r}/dir", None), True),
        ("os.mkdir", ("{r}/dir", 0o777, None), True),
        ("os.truncate", ("{r}/state.json", 0), True),
        ("os.symlink", ("{o}/target", "{r}/link", None), True),
        ("os.symlink", ("{r}/target", "{o}/link", None), True),  # an alias INTO the root
        ("os.symlink", ("{rp}", "{o}/alias", None), True),  # alias to an ANCESTOR of the root
        ("os.symlink", ("{o}/target", "{o}/alias", None), False),
        ("os.link", ("{r}/state.json", "{o}/alias", None, None), True),
        ("os.link", ("{o}/a", "{o}/b", None, None), False),
        ("shutil.copyfile", ("{o}/src", "{r}/dst"), True),
        ("shutil.copyfile", ("{r}/src", "{o}/dst"), False),
        ("shutil.move", ("{r}/src", "{o}/dst"), True),
        ("shutil.rmtree", ("{r}", None), True),
        ("sqlite3.connect", ("{r}/receipts.db",), True),
        ("sqlite3.connect", (":memory:",), False),
        ("sqlite3.connect", ("file:{r}/receipts.db",), True),
        ("sqlite3.connect", ("file://{r}/receipts.db?mode=rwc",), True),
        ("sqlite3.connect", ("file:{r}/receipts.db?mode=ro",), False),
        ("sqlite3.connect", ("file:{r}/scratch?mode=memory&cache=shared",), False),
        ("sqlite3.connect", ("file:{o}/receipts.db",), False),
        ("sqlite3.connect", ("file:{o}/x?cache=shared",), False),
        ("sqlite3.connect", ("file::memory:?cache=shared",), False),
        ("sqlite3.connect", ("",), False),  # a private temporary database
        ("sqlite3.connect", ("{r}/:memory:",), True),  # a real file that is named ':memory:'
        ("sqlite3.connect", ("{r}/x:memory:y",), True),
        # SQLite applies the LAST mode parameter: only a final ro/memory touches no file.
        ("sqlite3.connect", ("file:{r}/d.db?mode=memory&mode=rwc",), True),
        ("sqlite3.connect", ("file:{r}/d.db?mode=ro&mode=rwc",), True),
        ("sqlite3.connect", ("file:{r}/d.db?mode=rwc&mode=memory",), False),
        ("sqlite3.connect", ("file:{r}/d.db?mode=rwc&mode=ro",), False),
        # The audit event carries -1 for a dir_fd that was not given.
        ("os.mkdir", ("{r}/dir", 0o777, -1), True),
        ("os.remove", ("{r}/state.json", -1), True),
        ("os.rename", ("{o}/a", "{r}/b", -1, -1), True),
        ("os.chmod", ("{r}/state.json", 0o600, -1), True),
        ("os.chmod", ("{o}/state.json", 0o600, -1), False),
        ("os.chown", ("{r}/state.json", -1, -1, -1), True),
        ("os.chown", ("{o}/state.json", -1, -1, -1), False),
        ("os.utime", ("{r}/state.json", None, None, -1), True),
        ("os.utime", ("{o}/state.json", None, None, -1), False),
        ("shutil.copymode", ("{o}/src", "{r}/dst"), True),
        ("shutil.copymode", ("{r}/src", "{o}/dst"), False),
        ("shutil.copystat", ("{o}/src", "{r}/dst"), True),
        ("shutil.copystat", ("{r}/src", "{o}/dst"), False),
        ("shutil.copytree", ("{o}/src", "{r}/dst"), True),
        ("os.listdir", ("{r}",), False),
        ("socket.connect", (None, ("127.0.0.1", 1)), False),
    ],
)
def test_guard_event_table(event: str, args: tuple[Any, ...], denied: bool, tmp_path: Path) -> None:
    root = tmp_path / "real" / ".verdict"
    other = tmp_path / "other"
    guard = HomeWriteGuard([root])

    def fill(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.format(r=root, o=other, rn=root.relative_to(tmp_path), rp=root.parent)

    filled = tuple(fill(a) for a in args)
    if denied:
        with pytest.raises(PermissionError) as raised:
            guard.check(event, filled)
        assert raised.value.errno == errno.EACCES
        assert len(guard.violations) == 1 and guard.violations[0].event == event
    else:
        guard.check(event, filled)
        assert guard.violations == []


def test_renaming_a_prepared_tree_onto_an_ancestor_of_the_root_is_refused(tmp_path: Path) -> None:
    """``os.rename(tree, ~/.config)`` would create the protected ``~/.config/verdict``."""
    home = tmp_path / "home2"
    home.mkdir()
    tree = tmp_path / "tree"
    (tree / "verdict").mkdir(parents=True)
    (tree / "verdict" / "state.json").write_text("{}")
    root = home / ".config" / "verdict"
    with GUARD.protect(root):
        with pytest.raises(PermissionError):
            os.rename(tree, home / ".config")
        with pytest.raises(PermissionError):
            shutil.move(str(tree), str(home / ".config"))
        assert len(GUARD.drain()) == 2
        # An unrelated rename under the same home is still allowed.
        (home / "notes.txt").write_text("n")
        os.rename(home / "notes.txt", home / "notes.bak")
        assert not GUARD.violations
    assert not (home / ".config").exists()
    assert (tree / "verdict" / "state.json").read_text() == "{}"


def test_a_link_into_protected_state_is_refused_so_it_cannot_be_written_through(
    fake_real_home: Path, tmp_path: Path
) -> None:
    root = fake_real_home / ".verdict"
    protected_file = root / "model-metadata.json"
    alias = tmp_path / "alias.json"
    with GUARD.protect(root):
        with pytest.raises(PermissionError):
            os.link(protected_file, alias)
        with pytest.raises(PermissionError):
            os.symlink(protected_file, tmp_path / "symlink.json")
        found = GUARD.drain()
    assert len(found) == 2
    assert not alias.exists() and not (tmp_path / "symlink.json").is_symlink()
    assert protected_file.read_text() == '{"ratings": 1}'


def test_a_symlink_to_an_ancestor_of_the_protected_root_is_refused(
    fake_real_home: Path, tmp_path: Path
) -> None:
    """The alias would let ``alias/.verdict/state.json`` write through to the root."""
    root = fake_real_home / ".verdict"
    alias = tmp_path / "alias"
    with GUARD.protect(root):
        with pytest.raises(PermissionError):
            os.symlink(fake_real_home, alias)
        with pytest.raises(PermissionError):
            os.symlink("..", fake_real_home / ".verdict" / "up")  # also inside the root
        assert len(GUARD.drain()) == 2
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        os.symlink(unrelated, fake_real_home / "to-unrelated")  # an unrelated alias is fine
        assert not GUARD.violations
    assert not alias.is_symlink()


def test_relative_symlink_target_resolves_against_the_link_directory(
    fake_real_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fake_real_home / ".verdict"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # NOT the link's directory
    with GUARD.protect(root):
        # '.verdict' next to the link is the protected root, whatever the cwd is.
        with pytest.raises(PermissionError):
            os.symlink(".verdict", fake_real_home / "alias")
        GUARD.drain()
        assert not (fake_real_home / "alias").is_symlink()
        # The inverse: cwd inside the root, but the link (and so its target) is elsewhere.
        monkeypatch.chdir(root)
        os.symlink("model-metadata.json-not-protected", elsewhere / "fine")
        assert not GUARD.violations
        assert (elsewhere / "fine").is_symlink()


def test_in_memory_sqlite_uri_named_under_a_protected_root_is_allowed(fake_real_home: Path) -> None:
    root = fake_real_home / ".verdict"
    with GUARD.protect(root):
        connection = sqlite3.connect(f"file:{root}/scratch?mode=memory", uri=True)
        connection.execute("create table t (x)")
        connection.close()
        assert not GUARD.violations
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


def test_moving_or_deleting_an_ancestor_of_the_protected_root_is_refused(
    fake_real_home: Path, tmp_path: Path
) -> None:
    root = fake_real_home / ".verdict"
    elsewhere = tmp_path / "moved-home"
    with GUARD.protect(root):
        with pytest.raises(PermissionError):
            os.rename(fake_real_home, elsewhere)
        with pytest.raises(PermissionError):
            shutil.move(str(fake_real_home), str(elsewhere))
        with pytest.raises(PermissionError):
            shutil.rmtree(fake_real_home)
        found = GUARD.drain()
        # An unrelated sibling under the same home is still movable.
        (fake_real_home / "notes.txt").write_text("n")
        os.rename(fake_real_home / "notes.txt", fake_real_home / "notes.bak")
        assert not GUARD.violations
    assert len(found) == 3 and not elsewhere.exists()
    assert (root / "model-metadata.json").read_text() == '{"ratings": 1}'


def test_absolute_sqlite_uris_are_fine_when_cwd_is_inside_a_protected_root(
    fake_real_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a literal name that could really be created in cwd counts as one."""
    root = fake_real_home / ".verdict"
    monkeypatch.chdir(root)
    with GUARD.protect(root):
        for uri in (
            f"file:{tmp_path}/scratch?mode=memory",
            f"file:{tmp_path}/scratch?mode=memory&cache=shared",
            f"file:{tmp_path}/other.db?mode=ro",
        ):
            guard_only = HomeWriteGuard([root])
            guard_only.check("sqlite3.connect", (uri,))  # no violation
        scratch = sqlite3.connect(f"file:{tmp_path}/scratch?mode=memory", uri=True)
        scratch.close()
        assert not GUARD.violations
        # ...while a literal that CAN be created in the protected cwd stays refused,
        with pytest.raises(PermissionError):
            sqlite3.connect("file:state.db?mode=ro")
        # ...and so does a real write URI into the root.
        with pytest.raises(PermissionError):
            sqlite3.connect(f"file:{root}/uri.db", uri=True)
        assert len(GUARD.drain()) == 2
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


@pytest.mark.parametrize(
    "name",
    [":memory:", "", b":memory:", Path(":memory:")],
    ids=["memory", "empty-string-private-temp-db", "memory-bytes", "memory-pathlike"],
)
def test_sqlite_memory_and_private_temp_databases_are_not_files_in_a_protected_cwd(
    name: str | bytes | Path, fake_real_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``:memory:`` and ``''`` name no file, whatever the working directory is."""
    root = fake_real_home / ".verdict"
    monkeypatch.chdir(root)
    with GUARD.protect(root):
        connection = sqlite3.connect(name)
        connection.execute("create table t (x)")
        connection.close()
        assert not GUARD.violations
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


@pytest.mark.parametrize(
    "name",
    [
        "receipts.db",  # a real database in the working directory
        "./:memory:",  # SQLite's own spelling for a FILE named ':memory:'
        ":memory: ",  # not the special name: a file with a trailing space
        "x:memory:",
        ":memory:x",
        ":memory:?cache=shared",  # no file: prefix, so no URI reading
        "file:receipts.db",  # without uri=True this is a literal file in cwd
        "file::memory:",  # likewise: uri=False makes it a literal file in cwd
    ],
)
def test_sqlite_names_that_are_files_stay_guarded_when_cwd_is_a_protected_root(
    name: str, fake_real_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fake_real_home / ".verdict"
    monkeypatch.chdir(root)
    with GUARD.protect(root):
        with pytest.raises(PermissionError):
            sqlite3.connect(name)
        assert len(GUARD.drain()) == 1
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


def test_a_real_sqlite_database_path_under_a_protected_root_still_raises(
    fake_real_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fake_real_home / ".verdict"
    monkeypatch.chdir(root)
    with GUARD.protect(root):
        for name in (str(root / "receipts.db"), "receipts.db", Path("receipts.db")):
            with pytest.raises(PermissionError):
                sqlite3.connect(name)
        assert len(GUARD.drain()) == 3
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


def test_sqlite_uri_reading_of_the_memory_name_is_not_a_file_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit event does not say ``uri=``, so a ``file:`` string has two readings.

    As a URI ``file::memory:`` is the in-memory database and names no file. As a
    plain filename (``uri=False``) it is a file in cwd, which SQLite really
    creates: only that reading stays a target.
    """
    monkeypatch.chdir(tmp_path)
    targets = home_guard._sqlite_file_targets
    assert targets("file::memory:") == ["file::memory:"]
    assert targets("file::memory:?cache=shared") == ["file::memory:?cache=shared"]
    assert targets("file:%3Amemory%3A") == ["file:%3Amemory%3A"]
    # A path that merely ends in the name is a file: the decoded URI path stays a target.
    assert "/x/:memory:" in targets("file:/x/:memory:")
    assert targets(":memory:") == [] and targets("") == []
    assert targets("./:memory:") == ["./:memory:"]


def test_the_last_sqlite_mode_parameter_decides_whether_a_uri_touches_a_file(
    fake_real_home: Path,
) -> None:
    """``mode=memory&mode=rwc`` creates the file (SQLite applies the last ``mode``)."""
    root = fake_real_home / ".verdict"
    with GUARD.protect(root):
        with pytest.raises(PermissionError):
            sqlite3.connect(f"file:{root}/dup.db?mode=memory&mode=rwc", uri=True)
        GUARD.drain()
        scratch = sqlite3.connect(f"file:{root}/dup2.db?mode=rwc&mode=memory", uri=True)
        scratch.execute("create table t (x)")
        scratch.close()
        assert not GUARD.violations
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


def test_names_relative_to_a_dir_fd_are_not_resolved_against_the_cwd(
    fake_real_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``shutil.rmtree`` removes each entry by a bare name relative to an open directory."""
    root = fake_real_home / ".verdict"
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "x.txt").write_text("x")
    (tree / "y.txt").write_text("y")
    monkeypatch.chdir(root)  # a cwd reading would put 'x.txt' and 'y.txt' inside the root
    with GUARD.protect(root):
        shutil.rmtree(tree)
        assert not GUARD.violations
    assert not tree.exists()
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


def test_dir_fd_calls_are_checked_against_the_directory_the_descriptor_names(
    fake_real_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fake_real_home / ".verdict"
    (root / "keep").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "src.txt").write_text("s")
    monkeypatch.chdir(elsewhere)  # a cwd reading would allow every attempt below
    root_fd = os.open(root, os.O_RDONLY)  # opening a directory to read it is not a write
    other_fd = os.open(elsewhere, os.O_RDONLY)
    attempts = {
        "mkdir": lambda: os.mkdir("made", dir_fd=root_fd),
        "remove": lambda: os.remove("model-metadata.json", dir_fd=root_fd),
        "rmdir": lambda: os.rmdir("keep", dir_fd=root_fd),
        "rename into": lambda: os.rename(
            "src.txt", "moved.txt", src_dir_fd=other_fd, dst_dir_fd=root_fd
        ),
        "rename out of": lambda: os.rename(
            "model-metadata.json", "out.txt", src_dir_fd=root_fd, dst_dir_fd=other_fd
        ),
        "link": lambda: os.link("src.txt", "linked", src_dir_fd=other_fd, dst_dir_fd=root_fd),
        "symlink": lambda: os.symlink("target", "alias", dir_fd=root_fd),
        "chmod": lambda: os.chmod("model-metadata.json", 0o600, dir_fd=root_fd),
        "utime": lambda: os.utime("model-metadata.json", None, dir_fd=root_fd),
    }
    if sys.version_info >= (3, 11):  # shutil.rmtree has no dir_fd before 3.11
        attempts["rmtree"] = lambda: shutil.rmtree("keep", dir_fd=root_fd)
    try:
        with GUARD.protect(root):
            for label, attempt in attempts.items():
                with pytest.raises(PermissionError):
                    attempt()
                assert GUARD.violations, f"{label} was not reported"
                GUARD.drain()
            # The same calls relative to a directory outside the root are fine, and an
            # absolute path ignores the descriptor.
            os.mkdir("fine", dir_fd=other_fd)
            os.rename("src.txt", "dst.txt", src_dir_fd=other_fd, dst_dir_fd=other_fd)
            os.symlink("dst.txt", "alias", dir_fd=other_fd)
            os.remove("alias", dir_fd=other_fd)
            os.mkdir(str(elsewhere / "abs"), dir_fd=root_fd)
            assert not GUARD.violations
    finally:
        os.close(root_fd)
        os.close(other_fd)
    assert sorted(p.name for p in root.iterdir()) == ["keep", "model-metadata.json"]
    assert (root / "model-metadata.json").read_text() == '{"ratings": 1}'
    assert (elsewhere / "abs").is_dir() and (elsewhere / "dst.txt").is_file()


def test_a_closed_dir_fd_gets_the_operating_systems_own_error_not_a_guard_error(
    fake_real_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor that is not open names no directory: the OS refuses the call itself."""
    root = fake_real_home / ".verdict"
    monkeypatch.chdir(root)
    closed_fd = os.open(tmp_path, os.O_RDONLY)
    os.close(closed_fd)
    with GUARD.protect(root):
        with pytest.raises(OSError) as raised:
            os.mkdir("made", dir_fd=closed_fd)
        assert raised.value.errno == errno.EBADF
        assert not GUARD.violations
    assert sorted(p.name for p in root.iterdir()) == ["model-metadata.json"]


# ---------------------------------------------------------------------------
# Descriptor-relative calls: a name is read against the directory its descriptor names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirFds:
    root: Path  # the protected root
    elsewhere: Path  # an ordinary directory
    root_fd: int
    other_fd: int
    cwd: Path  # the working directory: inside neither


@pytest.fixture
def dir_fds(
    fake_real_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[DirFds]:
    """A protected root and an ordinary directory, each held open; the cwd is in neither."""
    root = fake_real_home / ".verdict"
    elsewhere = tmp_path / "elsewhere"
    cwd = tmp_path / "deep" / "cwd"
    for directory in (root, elsewhere):
        (directory / "sub").mkdir(parents=True, exist_ok=True)
        (directory / "src.txt").write_text("s")
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    other_fd = os.open(elsewhere, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with GUARD.protect(root):
            yield DirFds(root, elsewhere, root_fd, other_fd, cwd)
    finally:
        os.close(root_fd)
        os.close(other_fd)


def _listing(directory: Path) -> list[tuple[str, int, int, int]]:
    """Name, mode, size and mtime of every entry: any change to the directory shows."""
    found = []
    for entry in os.scandir(directory):
        info = entry.stat(follow_symlinks=False)
        found.append((entry.name, info.st_mode, info.st_size, info.st_mtime_ns))
    return sorted(found)


def _open_closed(name: str, flags: int, fd: int) -> int:
    descriptor = os.open(name, flags, 0o600, dir_fd=fd)
    os.close(descriptor)
    return descriptor


#: What each call below would do to the directory its descriptor names.
_DESCRIPTOR_CALLS: dict[str, Callable[[int], object]] = {
    "os.open (truncate)": lambda fd: _open_closed("src.txt", os.O_WRONLY | os.O_TRUNC, fd),
    "os.open (create)": lambda fd: _open_closed("made", os.O_WRONLY | os.O_CREAT, fd),
    "os.mkdir": lambda fd: os.mkdir("made", dir_fd=fd),
    "os.remove": lambda fd: os.remove("src.txt", dir_fd=fd),
    "os.rmdir": lambda fd: os.rmdir("sub", dir_fd=fd),
    "os.rename": lambda fd: os.rename("src.txt", "moved.txt", src_dir_fd=fd, dst_dir_fd=fd),
    "os.link": lambda fd: os.link("src.txt", "linked", src_dir_fd=fd, dst_dir_fd=fd),
    "os.symlink": lambda fd: os.symlink("src.txt", "alias", dir_fd=fd),
    "os.chmod": lambda fd: os.chmod("src.txt", 0o600, dir_fd=fd),
    "os.utime": lambda fd: os.utime("src.txt", None, dir_fd=fd),
    "os.chown": lambda fd: os.chown("src.txt", -1, -1, dir_fd=fd),
    "os.mkfifo": lambda fd: os.mkfifo("fifo", dir_fd=fd),
    "os.mknod": lambda fd: os.mknod("node", dir_fd=fd),
}
if sys.version_info >= (3, 11):  # shutil.rmtree has no dir_fd before 3.11
    _DESCRIPTOR_CALLS["shutil.rmtree"] = lambda fd: shutil.rmtree("sub", dir_fd=fd)

_WRITING_OPENS = {
    "truncate an existing file": ("model-metadata.json", os.O_WRONLY | os.O_TRUNC),
    "open an existing file read-write": ("model-metadata.json", os.O_RDWR),
    "append": ("model-metadata.json", os.O_WRONLY | os.O_APPEND),
    "create": ("made.json", os.O_WRONLY | os.O_CREAT),
    "create exclusive": ("made.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW),
}

_OPEN_SPELLINGS: dict[str, Callable[[str, int, int], int]] = {
    "positional mode": lambda name, flags, fd: os.open(name, flags, 0o600, dir_fd=fd),
    "default mode": lambda name, flags, fd: os.open(name, flags, dir_fd=fd),
    "keywords": lambda name, flags, fd: os.open(path=name, flags=flags, mode=0o600, dir_fd=fd),
    "bytes name": lambda name, flags, fd: os.open(os.fsencode(name), flags, dir_fd=fd),
    "path-like name": lambda name, flags, fd: os.open(Path(name), flags, dir_fd=fd),
}


@pytest.mark.parametrize("spelling", sorted(_OPEN_SPELLINGS))
@pytest.mark.parametrize("case", sorted(_WRITING_OPENS))
def test_a_writable_open_relative_to_a_dir_fd_cannot_reach_protected_state(
    dir_fds: DirFds, case: str, spelling: str
) -> None:
    """``os.open('state.json', O_TRUNC, dir_fd=root_fd)`` from a cwd outside the root."""
    name, flags = _WRITING_OPENS[case]
    metadata = dir_fds.root / "model-metadata.json"
    content, before = metadata.read_bytes(), _listing(dir_fds.root)
    with pytest.raises(PermissionError) as raised:
        _OPEN_SPELLINGS[spelling](name, flags, dir_fds.root_fd)
    assert raised.value.errno == errno.EACCES
    found = GUARD.drain()
    assert [(v.event, os.path.realpath(v.path)) for v in found] == [
        ("os.open", os.path.realpath(dir_fds.root / name))
    ]
    assert any("test_home_isolation.py" in frame for frame in found[0].where), found[0].where
    assert metadata.read_bytes() == content
    assert _listing(dir_fds.root) == before


class _Index:
    """An integer-like object: ``os`` takes it wherever it takes an int (``__index__``)."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __index__(self) -> int:
        return self.value


class _LyingInt(int):
    """An int whose ``__index__`` disagrees with its value: the call uses the value."""

    def __index__(self) -> int:
        return 0


#: How each integer parameter of ``os.open`` is spelled so that it is NOT an ``int``.
_INTEGER_LIKE_OPENS: dict[str, Callable[[str, int, int], int]] = {
    "flags": lambda name, flags, fd: os.open(name, _Index(flags), dir_fd=fd),
    "flags, positional mode": lambda name, flags, fd: os.open(
        name, _Index(flags), 0o600, dir_fd=fd
    ),
    "dir_fd": lambda name, flags, fd: os.open(name, flags, dir_fd=_Index(fd)),
    "mode": lambda name, flags, fd: os.open(name, flags, _Index(0o600), dir_fd=fd),
    "everything, keywords": lambda name, flags, fd: os.open(
        path=name, flags=_Index(flags), mode=_Index(0o600), dir_fd=_Index(fd)
    ),
    "flags an int subclass that lies": lambda name, flags, fd: os.open(
        name, _LyingInt(flags), dir_fd=fd
    ),
}


@pytest.mark.parametrize("spelling", sorted(_INTEGER_LIKE_OPENS))
@pytest.mark.parametrize("case", sorted(_WRITING_OPENS))
def test_integer_like_arguments_do_not_hide_a_writable_open_from_the_guard(
    dir_fds: DirFds, case: str, spelling: str
) -> None:
    """``flags`` (or ``dir_fd``) with ``__index__`` is converted by ``os.open``: the guard reads it too."""
    name, flags = _WRITING_OPENS[case]
    metadata = dir_fds.root / "model-metadata.json"
    content, before = metadata.read_bytes(), _listing(dir_fds.root)
    with pytest.raises(PermissionError) as raised:
        _INTEGER_LIKE_OPENS[spelling](name, flags, dir_fds.root_fd)
    assert raised.value.errno == errno.EACCES
    found = GUARD.drain()
    assert [(v.event, os.path.realpath(v.path)) for v in found] == [
        ("os.open", os.path.realpath(dir_fds.root / name))
    ]
    assert metadata.read_bytes() == content
    assert _listing(dir_fds.root) == before


@pytest.mark.parametrize("spelling", sorted(_INTEGER_LIKE_OPENS))
def test_an_integer_like_writable_open_outside_state_is_not_read_against_the_cwd(
    dir_fds: DirFds, spelling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(dir_fds.root)  # a cwd reading would put ``made.json`` in the protected root
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    os.close(_INTEGER_LIKE_OPENS[spelling]("made.json", flags, dir_fds.other_fd))
    assert (dir_fds.elsewhere / "made.json").is_file()
    assert not (dir_fds.root / "made.json").exists()
    assert not GUARD.violations


@pytest.mark.parametrize("spelling", sorted(_INTEGER_LIKE_OPENS))
def test_an_integer_like_open_that_does_not_write_stays_allowed_through_a_protected_dir_fd(
    dir_fds: DirFds, spelling: str
) -> None:
    os.close(_INTEGER_LIKE_OPENS[spelling]("model-metadata.json", os.O_RDONLY, dir_fds.root_fd))
    assert not GUARD.violations


class _IndexRaisesTypeError:
    def __index__(self) -> int:
        raise TypeError("not an integer after all")


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(object(), id="object"),
        pytest.param("w", id="str"),
        pytest.param(1.5, id="float"),
        pytest.param(None, id="None"),
        pytest.param(_IndexRaisesTypeError(), id="__index__ raises TypeError"),
    ],
)
def test_flags_that_are_not_integer_like_get_the_operating_systems_own_type_error(
    dir_fds: DirFds, flags: Any
) -> None:
    """The guard decides only for flags it can read; anything else is the call's own business."""
    with pytest.raises(TypeError) as direct:
        posix.open("model-metadata.json", flags, dir_fd=dir_fds.root_fd)
    with pytest.raises(TypeError) as guarded:
        os.open("model-metadata.json", flags, dir_fd=dir_fds.root_fd)
    assert str(guarded.value) == str(direct.value)
    assert not GUARD.violations


def test_an_index_that_raises_something_else_is_not_swallowed_by_the_guard(dir_fds: DirFds) -> None:
    class Broken:
        def __index__(self) -> int:
            raise ValueError("broken")

    flags: Any = Broken()
    with pytest.raises(ValueError, match="broken"):
        os.open("model-metadata.json", flags, dir_fd=dir_fds.root_fd)
    assert not GUARD.violations


@pytest.mark.parametrize("call", ["mkfifo", "mknod"])
def test_integer_like_mode_and_device_do_not_change_what_the_guard_does(
    dir_fds: DirFds, call: str
) -> None:
    """The guard never reads ``mode`` or ``device``: protected state is refused, the rest runs.

    (``device`` is left a plain int here: ``os.mknod`` itself refuses an ``__index__`` object
    for it, which ``dir_fd_probe`` compares with and without the guard.)
    """
    node = {
        "mkfifo": lambda fd, name: os.mkfifo(name, _Index(0o600), dir_fd=_Index(fd)),
        "mknod": lambda fd, name: os.mknod(
            name, _Index(stat.S_IFREG | 0o600), 0, dir_fd=_Index(fd)
        ),
    }[call]
    before = _listing(dir_fds.root)
    with pytest.raises(PermissionError):
        node(dir_fds.root_fd, "made")
    assert [v.event for v in GUARD.drain()] == [f"os.{call}"]
    assert _listing(dir_fds.root) == before
    node(dir_fds.other_fd, "made")
    assert stat.S_ISFIFO((dir_fds.elsewhere / "made").lstat().st_mode) == (call == "mkfifo")
    assert not GUARD.violations


def test_a_builtin_open_with_an_opener_is_read_against_the_openers_descriptor(
    dir_fds: DirFds,
) -> None:
    """The builtin's own ``open`` event carries the bare name; the opener's ``os.open`` is wrapped."""

    def opener(name: str, flags: int) -> int:
        return os.open(name, flags, 0o600, dir_fd=dir_fds.root_fd)

    with pytest.raises(PermissionError), open("made.json", "w", opener=opener):
        pass
    assert [v.event for v in GUARD.drain()] == ["os.open"]
    assert not (dir_fds.root / "made.json").exists()


def test_a_descriptor_relative_name_that_climbs_into_protected_state_is_refused(
    dir_fds: DirFds,
) -> None:
    climb = os.path.relpath(dir_fds.root / "model-metadata.json", dir_fds.elsewhere)
    assert climb.startswith("..")
    metadata = dir_fds.root / "model-metadata.json"
    content = metadata.read_bytes()
    with pytest.raises(PermissionError):
        os.open(climb, os.O_WRONLY | os.O_TRUNC, dir_fd=dir_fds.other_fd)
    assert GUARD.drain()
    assert metadata.read_bytes() == content


def test_opens_that_do_not_write_stay_allowed_through_a_protected_dir_fd(dir_fds: DirFds) -> None:
    kept = [
        os.open("model-metadata.json", os.O_RDONLY, dir_fd=dir_fds.root_fd),
        os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fds.root_fd),
        os.open("model-metadata.json", os.O_PATH | os.O_NOFOLLOW, dir_fd=dir_fds.root_fd),
    ]
    for descriptor in kept:
        os.close(descriptor)
    assert not GUARD.violations


def test_a_writable_open_relative_to_a_dir_fd_outside_state_is_not_read_against_the_cwd(
    dir_fds: DirFds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd reading would put ``made.json`` inside the protected root and refuse a fine call."""
    monkeypatch.chdir(dir_fds.root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    os.close(os.open("made.json", flags, 0o600, dir_fd=dir_fds.other_fd))
    assert (dir_fds.elsewhere / "made.json").is_file()
    assert not (dir_fds.root / "made.json").exists()
    assert not GUARD.violations


@pytest.mark.parametrize("kind", ["closed", "a file"])
@pytest.mark.parametrize("label", sorted(_DESCRIPTOR_CALLS))
def test_a_descriptor_that_is_no_open_directory_gets_the_operating_systems_own_error(
    dir_fds: DirFds, label: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EBADF / ENOTDIR from the OS, never a guard error, even with the cwd inside the root."""
    monkeypatch.chdir(dir_fds.root)  # a cwd reading would refuse every call below
    before = _listing(dir_fds.root)
    if kind == "closed":
        bad = os.open(dir_fds.elsewhere, os.O_RDONLY)
        os.close(bad)
        expected = errno.EBADF
    else:
        bad = os.open(dir_fds.elsewhere / "src.txt", os.O_RDONLY)
        expected = errno.ENOTDIR
    try:
        with pytest.raises(OSError) as raised:
            _DESCRIPTOR_CALLS[label](bad)
    finally:
        if kind == "a file":
            os.close(bad)
    assert raised.value.errno == expected, raised.value
    assert not GUARD.drain()
    assert _listing(dir_fds.root) == before


def _no_procfs(path: str) -> str:
    raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)


def _procfs_unreadable(path: str) -> str:
    raise PermissionError(errno.EACCES, "Permission denied", path)


def _procfs_names_no_path(path: str) -> str:
    return "anon_inode:[not-a-directory]"


_BROKEN_READLINKS = {
    "no procfs": _no_procfs,
    "inaccessible": _procfs_unreadable,
    "names no path": _procfs_names_no_path,
}


@pytest.mark.parametrize("broken", sorted(_BROKEN_READLINKS))
@pytest.mark.parametrize("target", ["protected", "ordinary"])
@pytest.mark.parametrize("label", sorted(_DESCRIPTOR_CALLS))
def test_a_directory_descriptor_that_cannot_be_resolved_is_refused_and_recorded(
    dir_fds: DirFds, label: str, target: str, broken: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard cannot say where the call lands, so it does not let it run."""
    directory, fd = {
        "protected": (dir_fds.root, dir_fds.root_fd),
        "ordinary": (dir_fds.elsewhere, dir_fds.other_fd),
    }[target]
    before = _listing(directory)
    monkeypatch.setattr(home_guard, "_readlink", _BROKEN_READLINKS[broken])
    with pytest.raises(PermissionError) as raised:
        _DESCRIPTOR_CALLS[label](fd)
    assert raised.value.errno == errno.EACCES
    found = GUARD.drain()
    assert len(found) == 1, found
    assert found[0].event == label.split(" ")[0]
    assert "dir_fd" in found[0].path and "/proc/self/fd" in found[0].path, found[0].path
    assert _listing(directory) == before


def test_a_broken_descriptor_lookup_refuses_nothing_that_needs_no_lookup(
    dir_fds: DirFds, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(home_guard, "_readlink", _no_procfs)
    os.close(os.open("src.txt", os.O_RDONLY, dir_fd=dir_fds.root_fd))  # a read
    elsewhere = dir_fds.elsewhere
    os.mkdir(str(elsewhere / "abs"), dir_fd=dir_fds.root_fd)  # absolute: the descriptor is ignored
    os.mkfifo(str(elsewhere / "abs-fifo"), dir_fd=dir_fds.root_fd)
    os.close(os.open(str(elsewhere / "abs-file"), os.O_WRONLY | os.O_CREAT, dir_fd=dir_fds.root_fd))
    os.mkdir(dir_fds.cwd / "plain")  # no descriptor at all
    assert not GUARD.violations


@pytest.mark.parametrize("make", ["mkfifo", "mknod"])
@pytest.mark.parametrize("how", ["absolute path", "relative to the cwd", "dir_fd"])
def test_special_files_cannot_be_created_in_protected_state(
    dir_fds: DirFds, make: str, how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.mkfifo`` and ``os.mknod`` raise no audit event: the session wraps them."""
    create = getattr(os, make)
    before = _listing(dir_fds.root)
    with pytest.raises(PermissionError):
        if how == "absolute path":
            create(dir_fds.root / "special")
        elif how == "relative to the cwd":
            monkeypatch.chdir(dir_fds.root)
            create("special")
        else:
            create("special", dir_fd=dir_fds.root_fd)
    found = GUARD.drain()
    assert [v.event for v in found] == [f"os.{make}"]
    assert _listing(dir_fds.root) == before
    create(dir_fds.elsewhere / "special")  # outside the root the same call is fine
    assert (dir_fds.elsewhere / "special").exists()


def test_the_probe_that_stops_a_session_without_descriptor_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert home_guard.descriptor_resolution_problem() is None
    with monkeypatch.context() as patched:
        patched.setattr(home_guard, "_PROC_FD_DIR", str(tmp_path / "no-proc"))
        problem = home_guard.descriptor_resolution_problem()
        assert problem and str(tmp_path / "no-proc") in problem
    with monkeypatch.context() as patched:
        patched.setattr(home_guard, "_readlink", _procfs_unreadable)
        assert "Permission denied" in (home_guard.descriptor_resolution_problem() or "")
    with monkeypatch.context() as patched:
        patched.setattr(home_guard, "_readlink", lambda path: "/somewhere/else")
        assert "/somewhere/else" in (home_guard.descriptor_resolution_problem() or "")
    assert home_guard.descriptor_resolution_problem() is None


# How the guard covers every ``os`` function that takes a ``dir_fd``, checked against the
# interpreter that runs the tests (``dir_fd_probe events`` raises the real audit events).


def _probe(*argv: str, home: Path, guard_env: bool = False) -> dict[str, Any]:
    """Run ``tests.helpers.dir_fd_probe`` in a fresh interpreter; its last stdout line is JSON."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in home_guard.HOST_STATE_ENV and not key.startswith("PYTEST_")
    }
    env.update(HOME=str(home), PYTHONPATH=str(REPO_ROOT))
    home.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.dir_fd_probe", *argv],
        cwd=home,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    document: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return document


def test_every_os_function_that_takes_a_dir_fd_is_classified() -> None:
    members = {function.__name__ for function in os.supports_dir_fd}
    assert members <= set(home_guard.DIR_FD_COVERAGE), (
        f"classify {sorted(members - set(home_guard.DIR_FD_COVERAGE))} in DIR_FD_COVERAGE"
    )
    for name, (how, detail) in home_guard.DIR_FD_COVERAGE.items():
        assert how in {"read-only", "audit event", "wrapper"}, name
        if how == "audit event":
            assert detail in home_guard._DIR_FD_ARGS, name
        if how == "wrapper":
            assert detail in home_guard.DESCRIPTOR_CALLS, name
    assert {n for n, (how, _) in home_guard.DIR_FD_COVERAGE.items() if how == "wrapper"} == set(
        home_guard.DESCRIPTOR_CALLS
    )


def test_the_audit_events_the_coverage_table_relies_on_carry_the_descriptor(tmp_path: Path) -> None:
    """Each "audit event" row raises that event, with the descriptor where ``_DIR_FD_ARGS`` says."""
    seen = _probe("events", home=tmp_path / "home")
    for member, (how, event) in home_guard.DIR_FD_COVERAGE.items():
        if member not in seen["calls"] or how != "audit event":
            continue
        raised = [args for name, args in seen["calls"][member] if name == event]
        assert raised, f"os.{member} raised no {event} event: {seen['calls'][member]}"
        for path_index, fd_index in home_guard._DIR_FD_ARGS[event].items():
            assert raised[0][fd_index] == seen["dir_fd"], (member, event, raised[0])
            assert isinstance(raised[0][path_index], str)
    for member, (how, _) in home_guard.DIR_FD_COVERAGE.items():
        if member in seen["calls"] and how == "wrapper":
            names = {name for name, _ in seen["calls"][member]}
            # the wrapped call's own event (``open``) does not carry the descriptor; the
            # others raise none: either way the wrapper is what reads the descriptor
            assert not (names & set(home_guard._DIR_FD_ARGS)), (member, names)


def test_the_guard_does_not_change_what_descriptor_calls_do(tmp_path: Path) -> None:
    """Same calls, with and without the guard: same results, exceptions, errnos and files."""
    plain = _probe("results", home=tmp_path / "plain-home")
    guarded = _probe("results", "--guard", home=tmp_path / "guarded-home")
    # The comparison is only worth anything if the guard really was in the second interpreter.
    assert plain["engaged"] == {
        "descriptor write": "allowed",
        "integer-like flags write": "allowed",
        "path write": "allowed",
        "wrapped": {"mkfifo": False, "mknod": False, "open": False},
    }
    assert guarded["engaged"] == {
        "descriptor write": "refused",
        "integer-like flags write": "refused",
        "path write": "refused",
        "wrapped": {"mkfifo": True, "mknod": True, "open": True},
    }
    different = {
        label: (plain["results"][label], outcome)
        for label, outcome in guarded["results"].items()
        if plain["results"][label] != outcome
    }
    assert not different, different
    assert guarded["results"].keys() == plain["results"].keys()
    assert guarded["listing"] == plain["listing"]
    assert len(plain["results"]) > 60


def test_the_descriptor_wrappers_live_exactly_as_long_as_the_guard_plugin(tmp_path: Path) -> None:
    """Installed for a pytest run, gone after it, and a second run in the same process works."""
    work = tmp_path / "work"
    work.mkdir()
    report = _probe("lifecycle", str(work), home=tmp_path / "home")
    assert report["runs"] == [
        {"exit": 0, "restored": True, "membership": True},
        {"exit": 0, "restored": True, "membership": True},
    ]


def test_double_leading_slash_cannot_reach_a_protected_root(fake_real_home: Path) -> None:
    root = fake_real_home / ".verdict"
    doubled = "/" + str(root / "state.json")  # '//tmp/...': the same file on Linux
    assert doubled.startswith("//")
    with GUARD.protect(root):
        with pytest.raises(PermissionError), open(doubled, "w"):
            pass
        with pytest.raises(PermissionError):
            os.mkdir("/" + str(root / "dir"))
        GUARD.drain()
    assert list(root.glob("state.json")) == []


def test_exported_xdg_bases_protect_only_verdicts_own_subdirectories(tmp_path: Path) -> None:
    config, data = tmp_path / "operator-config", tmp_path / "operator-data"
    roots = home_guard.real_state_roots(
        {"HOME": str(tmp_path / "h"), "XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(data)}
    )
    assert str(config / "verdict") in roots
    assert str(config / "systemd" / "user") in roots
    assert str(data / "verdict") in roots
    assert str(config) not in roots and str(data) not in roots
    assert str(tmp_path / "h" / ".verdict") in roots


def test_relocated_verdict_config_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XDG_CONFIG_HOME exported at start-up: the config path it relocates stays protected."""
    from verdict.setup_config import config_path

    operator_config = tmp_path / "operator-config"
    environ = {"HOME": str(tmp_path / "h"), "XDG_CONFIG_HOME": str(operator_config)}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(operator_config))  # as the operator's shell had it
    target = config_path()
    assert target == operator_config / "verdict" / "verdict.yaml"
    guard = HomeWriteGuard(home_guard.real_state_roots(environ))
    with pytest.raises(PermissionError):
        guard.check("open", (str(target), "w", _flags("O_WRONLY", "O_CREAT", "O_TRUNC")))
    guard.check(
        "open", (str(operator_config / "other-tool" / "c.yaml"), "w", _flags("O_WRONLY", "O_CREAT"))
    )


def test_guard_checks_the_literal_filename_of_a_read_only_sqlite_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``uri=True`` the string is a writable literal filename in cwd."""
    root = tmp_path / "real" / ".verdict"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    guard = HomeWriteGuard([root])
    with pytest.raises(PermissionError):
        guard.check("sqlite3.connect", ("file:state.db?mode=ro",))
    with GUARD.protect(root):
        with pytest.raises(PermissionError):
            sqlite3.connect("file:state.db?mode=ro")
        GUARD.drain()
    assert list(root.iterdir()) == []


def test_guard_resolves_relative_paths_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "real" / ".verdict"
    root.mkdir(parents=True)
    guard = HomeWriteGuard([root])
    monkeypatch.chdir(root)
    with pytest.raises(PermissionError):
        guard.check("open", ("state.json", "w", _flags("O_WRONLY", "O_CREAT")))


def test_guard_prevents_every_write_route_to_the_fake_real_home(
    fake_real_home: Path, tmp_path: Path
) -> None:
    """Through the installed audit hook: refused before the write, and reported."""
    root = fake_real_home / ".verdict"
    metadata = root / "model-metadata.json"
    before = metadata.read_bytes()
    source = tmp_path / "source.json"
    source.write_text("source")
    existing_db = root / "existing.db"
    sqlite3.connect(existing_db).close()  # created before the root is protected

    attempts = {
        "pathlib write": lambda: (root / "state.json").write_text("leak"),
        "open append": lambda: open(root / "log.jsonl", "a").close(),
        "open overwrite": lambda: open(metadata, "w").close(),
        "os.open create": lambda: os.close(os.open(root / "made", os.O_WRONLY | os.O_CREAT, 0o600)),
        "mkdir": lambda: (root / "prove-at-rest").mkdir(),
        "rename into": lambda: os.rename(source, root / "renamed.json"),
        "replace over": lambda: os.replace(source, metadata),
        "copyfile": lambda: shutil.copyfile(source, root / "copied.json"),
        "remove": lambda: os.remove(metadata),
        "sqlite create": lambda: sqlite3.connect(root / "receipts.db"),
        "sqlite uri create": lambda: sqlite3.connect(f"file:{root}/uri.db", uri=True),
        "sqlite uri percent-escaped": lambda: sqlite3.connect(
            f"file:{str(root).replace('-', '%2D')}/uri2.db", uri=True
        ),
    }
    with GUARD.protect(root):
        for label, attempt in attempts.items():
            with pytest.raises(PermissionError):
                attempt()
            assert GUARD.violations, f"{label} was not reported"
            GUARD.drain()
        # Reading is untouched: the guard refuses writes only.
        readonly = sqlite3.connect(f"file:{existing_db}?mode=ro", uri=True)
        readonly.execute("select 1").fetchall()
        readonly.close()
        assert metadata.read_bytes() == before
        assert sorted(p.name for p in root.iterdir()) == ["existing.db", "model-metadata.json"]
        assert source.read_text() == "source"


def test_guard_catches_the_incident_write_to_the_fake_real_home(fake_real_home: Path) -> None:
    """The write that overwrote ~/.verdict/prove-at-rest/state.json on 2026-11-23."""
    from datetime import datetime, timezone

    from verdict.prove_at_rest_legacy import ProveAtRestCycle, ProveAtRestStore

    state = fake_real_home / ".verdict" / "prove-at-rest" / "state.json"
    when = datetime(2026, 2, 1, tzinfo=timezone.utc)
    cycle = ProveAtRestCycle(cycle_id="test-cycle", started_at=when, finished_at=when, results=())
    with GUARD.protect(fake_real_home / ".verdict"):
        with pytest.raises(PermissionError):
            ProveAtRestStore(path=state).write(cycle)
        found = GUARD.drain()
    assert not state.exists()
    assert found and all(str(state.parent) in v.path or str(state) in v.path for v in found)
    assert any("prove_at_rest" in frame for v in found for frame in v.where)


def test_guard_reports_the_test_that_leaked_even_if_the_error_is_swallowed(
    fake_real_home: Path, request: pytest.FixtureRequest
) -> None:
    """The per-test fixture, not the exception, is what fails a leaking test."""
    root = fake_real_home / ".verdict"
    with GUARD.protect(root):
        with contextlib.suppress(OSError):  # production code that tolerates write failures
            (root / "swallowed.json").write_text("x")
        found = GUARD.drain()
    assert [v.path for v in found] == [str(root / "swallowed.json")]
    assert found[0].phase == request.node.nodeid  # attributed to this test, by name


def test_real_roots_were_resolved_at_import_not_from_the_per_test_home() -> None:
    assert GUARD.roots, "no protected roots"
    home = str(_per_test_home())
    assert not any(root.startswith(home) for root in GUARD.roots)
    assert str(Path(os.environ["HOME"]) / ".verdict") not in GUARD.roots


# ---------------------------------------------------------------------------
# Enforcement end to end: a child pytest run with deliberately leaking tests
# ---------------------------------------------------------------------------

_GUARD_ONLY_CONFTEST = "from tests.helpers.home_guard import pytest_configure  # noqa: F401\n"


def _child_pytest(
    workdir: Path,
    files: dict[str, str],
    *,
    home: Path,
    extra_env: dict[str, str] | None = None,
    args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    for name, body in files.items():
        (workdir / name).write_text(textwrap.dedent(body))
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in home_guard.HOST_STATE_ENV and not key.startswith("PYTEST_")
    }
    env.update(HOME=str(home), PYTHONPATH=str(REPO_ROOT), **(extra_env or {}))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *args],
        cwd=workdir,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_a_leaking_test_fails_the_run_and_names_the_file(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    fake_home = tmp_path / "fake-real-home"
    fake_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    result = _child_pytest(
        work,
        {
            "conftest.py": _GUARD_ONLY_CONFTEST,
            "test_leaky.py": """
                import os, pathlib
                import pytest

                def _state():
                    return pathlib.Path(os.environ["HOME"]) / ".verdict" / "prove-at-rest"

                def test_leaks_loudly():
                    _state().mkdir(parents=True)

                def test_leaks_and_swallows_the_error():
                    try:
                        (pathlib.Path(os.environ["HOME"]) / ".verdict" / "state.json").write_text("x")
                    except OSError:
                        pass

                @pytest.fixture(scope="module", autouse=True)
                def leaky_module_fixture():
                    try:
                        (pathlib.Path(os.environ["HOME"]) / ".verdict" / "fixture.json").write_text("x")
                    except OSError:
                        pass

                def test_clean(tmp_path):
                    (tmp_path / "ok.txt").write_text("fine")
            """,
        },
        home=fake_home,
        args=args,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "test_leaky.py::test_leaks_loudly" in output
    assert "test_leaky.py::test_leaks_and_swallows_the_error" in output
    assert str(fake_home / ".verdict" / "state.json") in output
    assert str(fake_home / ".verdict" / "prove-at-rest") in output
    assert "outside any test" in output  # the module fixture's refused write
    assert "nothing was written" in output
    assert not (fake_home / ".verdict").exists(), "the guard let a write through"


@pytest.mark.parametrize("args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_a_clean_run_passes_with_the_guard_loaded(tmp_path: Path, args: tuple[str, ...]) -> None:
    fake_home = tmp_path / "fake-real-home"
    fake_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    result = _child_pytest(
        work,
        {
            "conftest.py": _GUARD_ONLY_CONFTEST,
            "test_clean.py": """
                import os

                def test_reads_are_fine():
                    os.listdir(os.environ["HOME"])

                def test_tmp_writes_are_fine(tmp_path):
                    (tmp_path / "state.json").write_text("{}")
            """,
        },
        home=fake_home,
        args=args,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_XDIST_ARGS = ("-n", "2")


@pytest.mark.parametrize("args", [(), _XDIST_ARGS], ids=["serial", "xdist"])
def test_a_write_after_the_last_test_still_fails_the_run(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    fake_home = tmp_path / "fake-real-home"
    fake_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    result = _child_pytest(
        work,
        {
            "conftest.py": _GUARD_ONLY_CONFTEST,
            "test_late.py": """
                import os, pathlib
                import pytest

                @pytest.fixture(scope="session", autouse=True)
                def writes_at_session_teardown():
                    yield
                    try:
                        (pathlib.Path(os.environ["HOME"]) / ".verdict" / "late.json").write_text("x")
                    except OSError:
                        pass

                def test_passes():
                    pass
            """,
        },
        home=fake_home,
        args=args,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "late.json" in output
    assert not (fake_home / ".verdict").exists()


def _wire(*found: home_guard.Violation) -> dict[str, object]:
    return {home_guard.WORKER_STATUS_KEY: home_guard.worker_status(list(found))}


def test_a_worker_that_reported_clean_has_no_findings() -> None:
    assert home_guard.worker_findings(_wire(), "gw0") == []


def test_a_worker_violation_survives_the_wire_and_names_worker_and_file() -> None:
    violation = home_guard.Violation(
        "OUTSIDE", "open", "/real/.verdict/late.json", ("a.py:1 in f",)
    )
    findings = home_guard.worker_findings(_wire(violation), "gw1")
    assert len(findings) == 1
    assert "gw1" in findings[0] and "/real/.verdict/late.json" in findings[0]
    assert "a.py:1 in f" in findings[0]


@pytest.mark.parametrize(
    "output",
    [
        None,
        {},
        {"exitstatus": 0},
        "clean",
        {home_guard.WORKER_STATUS_KEY: None},
        {home_guard.WORKER_STATUS_KEY: "none"},
        {home_guard.WORKER_STATUS_KEY: [{"phase": "p"}]},
        {home_guard.WORKER_STATUS_KEY: [None]},
    ],
    ids=["none", "empty", "no-key", "not-a-dict", "null", "str", "missing-fields", "null-entry"],
)
def test_a_worker_status_the_controller_cannot_read_fails_closed(output: object) -> None:
    assert home_guard.worker_findings(output, "gw0")


def test_a_worker_that_went_down_fails_closed_even_with_a_clean_report() -> None:
    assert home_guard.worker_findings(_wire(), "gw0", errored=True)


# What an exported value must NOT make the guard protect, pinned here independently of
# ``home_guard.NOT_PROTECTED_ENV`` so loosening the rule is a visible two-sided edit. Every
# other ``HOST_STATE_ENV`` member whose exported value names a file or directory is protected.
_UNPROTECTED_STATE_ENV = {
    "VERDICT_RELAY_ADMISSION_CACHE": "boolean switch",
    "VERDICT_DECISION_SIGNALS_MODE": "mode name",
    "VERDICT_GATEWAY": "URL",
    "VERDICT_SHARED_MEMORY_URL": "URL",
    "VERDICT_SHARED_MEMORY_TOKEN": "secret",
    "VERDICT_ACTIVE_CONTROLLER_ROUTE": "route name",
    "VERDICT_OPENCODE_BIN": "binary resolved on PATH",
    "VERDICT_SERVE_URL": "URL",
    "VERDICT_CONTEXT_ROOTS": "comma-separated names relative to the workspace",
    "VERDICT_MCP_CONTEXT_ROOT": "read-only source-tree selector",
    "VERDICT_WORKSPACE_ROOT": "read-only source-tree selector",
}
_XDG_BASES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
_PROTECTED_STATE_ENV = tuple(
    name
    for name in home_guard.HOST_STATE_ENV
    if name not in _UNPROTECTED_STATE_ENV and name not in _XDG_BASES
)


def test_the_state_env_the_guard_protects_is_pinned() -> None:
    assert {"VERDICT_CLAUDE_OUTBOX_DIR", "VERDICT_CODEX_OUTBOX_DIR"} <= set(_PROTECTED_STATE_ENV)
    assert set(home_guard.NOT_PROTECTED_ENV) == set(_UNPROTECTED_STATE_ENV)
    assert set(home_guard.NOT_PROTECTED_ENV) <= set(home_guard.HOST_STATE_ENV), "stale entry"
    assert all(home_guard.NOT_PROTECTED_ENV.values()), "every skip names its reason"
    assert home_guard.protected_state_env() == _PROTECTED_STATE_ENV


def _environ_exporting(tmp_path: Path) -> dict[str, str]:
    environ = {
        name: str(tmp_path / "exported" / name.lower()) for name in home_guard.HOST_STATE_ENV
    }
    environ["HOME"] = str(tmp_path / "home")
    return environ


def test_every_exported_location_in_host_state_env_is_a_protected_root(tmp_path: Path) -> None:
    environ = _environ_exporting(tmp_path)
    roots = home_guard.real_state_roots(environ)
    for name in _PROTECTED_STATE_ENV:
        assert environ[name] in roots, f"{name} names a location and is not protected"
    for name in _UNPROTECTED_STATE_ENV:
        assert environ[name] not in roots, f"{name} names no state and must stay writable"
    for name in _XDG_BASES:  # a shared base is never protected whole: only verdict's own part
        assert environ[name] not in roots
        assert os.path.join(environ[name], "verdict") in roots
    assert str(tmp_path / "home" / ".verdict") in roots


def test_a_variable_added_to_host_state_env_is_protected_without_a_second_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        home_guard, "HOST_STATE_ENV", (*home_guard.HOST_STATE_ENV, "VERDICT_NEW_THING_DIR")
    )
    assert "VERDICT_NEW_THING_DIR" in home_guard.protected_state_env()
    where = str(tmp_path / "new-thing")
    assert where in home_guard.real_state_roots(
        {"HOME": str(tmp_path), "VERDICT_NEW_THING_DIR": where}
    )


@pytest.mark.parametrize(
    "value", ["", "   ", ":memory:", "http://127.0.0.1:20128", "redis://cache/0"], ids=repr
)
def test_a_value_that_is_not_a_location_adds_no_root(tmp_path: Path, value: str) -> None:
    base = {"HOME": str(tmp_path)}
    expected = home_guard.real_state_roots(base)
    for name in ("VERDICT_EVIDENCE_DB", "VERDICT_CLAUDE_OUTBOX_DIR", "VERDICT_RUNS_DIR"):
        assert home_guard.real_state_roots({**base, name: value}) == expected, name


def test_a_relative_export_is_protected_where_it_points_at_session_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    environ = {"HOME": "/h", "VERDICT_CODEX_OUTBOX_DIR": "outbox/codex"}
    guard = HomeWriteGuard(home_guard.real_state_roots(environ))
    assert os.path.join(os.getcwd(), "outbox", "codex") in guard.roots
    monkeypatch.chdir("/")  # later chdir does not move the protected location
    with pytest.raises(PermissionError):
        guard.check("open", (str(tmp_path / "outbox" / "codex" / "x"), "w", _flags("O_WRONLY")))


def test_outcome_shadow_roots_are_a_path_list_and_its_sink_is_protected(tmp_path: Path) -> None:
    first, second = tmp_path / "runs" / "a", tmp_path / "other" / "b"
    roots = home_guard.real_state_roots(
        {
            "HOME": str(tmp_path / "h"),
            "VERDICT_OUTCOME_SHADOW_ROOTS": os.pathsep.join([str(first), "", str(second)]),
        }
    )
    assert str(first) in roots and str(second) in roots
    # verdict appends its sink beside the FIRST entry (outcome_shadow_sink_path).
    assert str(first.parent / "outcome_shadow.jsonl") in roots
    assert str(second.parent / "outcome_shadow.jsonl") not in roots


def test_the_shadow_sink_name_matches_what_verdict_writes() -> None:
    from verdict.outcome_shadow import SINK_FILENAME

    assert home_guard.OUTCOME_SHADOW_SINK == SINK_FILENAME


@pytest.mark.parametrize(
    "value",
    ["/", "/tmp", "/tmp/", "//tmp", "/var/tmp", str(REPO_ROOT), str(REPO_ROOT.parent), "."],
    ids=["root", "tmp", "tmp-slash", "tmp-double-slash", "var-tmp", "repo", "above-repo", "cwd"],
)
@pytest.mark.parametrize("name", ["VERDICT_CLAUDE_OUTBOX_DIR", "VERDICT_RUNS_DIR", "CLAUDE_HOME"])
def test_an_export_that_would_cover_ordinary_writes_is_a_named_usage_problem(
    name: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    problems = home_guard.unsafe_state_roots({"HOME": "/h", name: value})
    assert len(problems) == 1 and problems[0].startswith(f"{name}="), problems


@pytest.mark.parametrize("where", ["temp dir", "cwd", "above cwd"])
def test_the_temp_dir_and_cwd_of_this_run_are_never_protectable(
    where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmpdir = tmp_path / "tmpdir"
    workdir = tmp_path / "a" / "work"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)
    value = {"temp dir": tmpdir, "cwd": workdir, "above cwd": workdir.parent}[where]
    environ = {"HOME": "/h", "TMPDIR": str(tmpdir), "VERDICT_CODEX_OUTBOX_DIR": str(value)}
    assert home_guard.unsafe_state_roots(environ)


def test_a_dedicated_directory_is_not_a_usage_problem(tmp_path: Path) -> None:
    environ = _environ_exporting(tmp_path)
    environ.update(VERDICT_CLAUDE_OUTBOX_DIR="/tmp/operator-outbox", VERDICT_EVIDENCE_DB=":memory:")
    assert home_guard.unsafe_state_roots(environ) == []
    assert home_guard.STARTUP_PROBLEMS == (), "this run was started with an overbroad export"


def _protected_state_probe(exported: Path) -> str:
    return f"""
        import os, pathlib
        import pytest
        from tests.helpers.home_guard import GUARD, HOST_STATE_ENV

        EXPORTED = pathlib.Path({str(exported)!r})
        PROTECTED = {list(_PROTECTED_STATE_ENV)!r}
        UNPROTECTED = {sorted(_UNPROTECTED_STATE_ENV)!r}
        XDG = {list(_XDG_BASES)!r}
        # What the operator's shell exported, read at collection time, before any fixture
        # clears it: the way a module-level constant captures it.
        CAPTURED = {{n: os.environ[n] for n in HOST_STATE_ENV if n in os.environ}}

        def test_no_exported_state_env_reaches_the_test():
            assert [n for n in HOST_STATE_ENV if n in os.environ] == []

        def test_default_paths_land_under_the_per_test_home():
            from verdict.paths import verdict_home
            assert str(verdict_home()).startswith(os.environ["HOME"])

        @pytest.mark.parametrize("name", PROTECTED)
        def test_the_exported_location_is_still_refused(name):
            target = pathlib.Path(CAPTURED[name])
            assert target.parent == EXPORTED
            for victim in (target, target / "x.json"):
                with pytest.raises(PermissionError):
                    victim.write_text("x")
                assert GUARD.drain(), "the write was refused but not reported"
            assert not target.exists()

        @pytest.mark.parametrize("name", UNPROTECTED)
        def test_a_variable_that_names_no_state_stays_writable(name):
            target = pathlib.Path(CAPTURED[name])
            target.mkdir()
            (target / "x.json").write_text("x")

        @pytest.mark.parametrize("name", XDG)
        def test_an_exported_xdg_base_refuses_only_verdicts_own_part(name):
            base = pathlib.Path(CAPTURED[name])
            (base / "other-tool").mkdir(parents=True)
            (base / "other-tool" / "c.json").write_text("x")
            with pytest.raises(PermissionError):
                (base / "verdict" / "x.json").write_text("x")
            assert GUARD.drain()
    """


@pytest.mark.parametrize("args", [(), _XDIST_ARGS], ids=["serial", "xdist"])
def test_exported_state_env_is_cleared_and_still_protected(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    """The real conftest, with the operator's state env exported, in a child run.

    Every exported location, outbox directories included, stays refused for a test that
    captured it before the isolation fixtures cleared the variable.
    """
    fake_home = tmp_path / "fake-real-home"
    fake_home.mkdir()
    exported = tmp_path / "exported-state"
    exported.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(REPO_ROOT / "tests" / "conftest.py", work / "conftest.py")
    polluted = {name: str(exported / name.lower()) for name in home_guard.HOST_STATE_ENV}
    result = _child_pytest(
        work,
        {"test_probe.py": _protected_state_probe(exported)},
        home=fake_home,
        extra_env=polluted,
        args=args,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    expected = 2 + len(_PROTECTED_STATE_ENV) + len(_UNPROTECTED_STATE_ENV) + len(_XDG_BASES)
    assert f"{expected} passed" in result.stdout, output
    for name in _PROTECTED_STATE_ENV:
        assert not (exported / name.lower()).exists(), f"a write reached {name}"


@pytest.mark.parametrize("args", [(), _XDIST_ARGS], ids=["serial", "xdist"])
def test_an_export_that_would_cover_the_temp_dir_stops_the_run_naming_the_variable(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    fake_home = tmp_path / "fake-real-home"
    fake_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "ran.marker"
    result = _child_pytest(
        work,
        {
            "conftest.py": _GUARD_ONLY_CONFTEST,
            "test_never_runs.py": f"""
                import pathlib

                def test_would_write_to_tmp():
                    pathlib.Path({str(marker)!r}).write_text("ran")
            """,
        },
        home=fake_home,
        extra_env={"VERDICT_CLAUDE_OUTBOX_DIR": "/tmp"},
        args=args,
    )
    output = result.stdout + result.stderr
    assert result.returncode == int(pytest.ExitCode.USAGE_ERROR), output
    assert "VERDICT_CLAUDE_OUTBOX_DIR=/tmp" in output, output
    assert "usage" in output.lower() or "ERROR" in output, output
    assert not marker.exists(), "a test ran although the guard could not start safely"


@pytest.mark.parametrize("args", [(), _XDIST_ARGS], ids=["serial", "xdist"])
def test_a_session_that_cannot_resolve_descriptors_stops_and_says_why(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    fake_home = tmp_path / "fake-real-home"
    fake_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "ran.marker"
    result = _child_pytest(
        work,
        {
            "conftest.py": """
                import tests.helpers.home_guard as home_guard

                home_guard._PROC_FD_DIR = "/nonexistent/proc/self/fd"
                from tests.helpers.home_guard import pytest_configure  # noqa: F401
            """,
            "test_never_runs.py": f"""
                import pathlib

                def test_would_run_unguarded():
                    pathlib.Path({str(marker)!r}).write_text("ran")
            """,
        },
        home=fake_home,
        args=args,
    )
    output = result.stdout + result.stderr
    assert result.returncode == int(pytest.ExitCode.USAGE_ERROR), output
    assert "/nonexistent/proc/self/fd" in output, output
    assert "dir_fd" in output, output
    assert not marker.exists(), "a test ran although descriptors could not be resolved"


# ---------------------------------------------------------------------------
# Detection: the throwaway-home snapshot
# ---------------------------------------------------------------------------


def test_snapshot_diff_reports_created_modified_and_deleted(tmp_path: Path) -> None:
    root = tmp_path / "home"
    (root / ".verdict" / "sub").mkdir(parents=True)
    (root / ".verdict" / "keep.json").write_text("keep")
    (root / ".verdict" / "change.json").write_text("one")
    (root / ".verdict" / "gone.json").write_text("gone")
    before = snapshot(root)

    (root / ".verdict" / "change.json").write_text("two")
    (root / ".verdict" / "gone.json").unlink()
    (root / ".verdict" / "sub" / "new.json").write_text("new")
    found = diff(before, snapshot(root))

    assert found.created == (".verdict/sub/new.json",)
    assert found.modified == (".verdict/change.json",)
    assert found.deleted == (".verdict/gone.json",)
    assert bool(found)
    assert not diff(before, before)


def test_snapshot_hashes_but_never_exposes_contents(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    (root / "secret.json").write_text("sk-live-do-not-print")
    shot = snapshot(root)
    assert shot["secret.json"].digest and len(shot["secret.json"].digest) == 64
    assert "sk-live-do-not-print" not in repr(shot)


def test_snapshot_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.json").write_text("x")
    root = tmp_path / "home"
    root.mkdir()
    (root / "link").symlink_to(outside)
    shot = snapshot(root)
    assert set(shot) == {"link"} and shot["link"].kind == "symlink"
    (outside / "real.json").write_text("changed")
    assert not diff(shot, snapshot(root))


def test_detector_plugin_names_the_leaking_test_and_not_the_clean_one(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel-home"
    (sentinel / ".verdict").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    report = tmp_path / "leaks.json"
    result = _child_pytest(
        work,
        {
            "test_probe.py": f"""
                import pathlib

                SENTINEL = pathlib.Path({str(sentinel)!r})

                def test_clean(tmp_path):
                    (tmp_path / "ok.txt").write_text("fine")

                def test_leaks():
                    (SENTINEL / ".verdict" / "prove-at-rest").mkdir()
                    (SENTINEL / ".verdict" / "prove-at-rest" / "state.json").write_text("{{}}")
            """
        },
        home=tmp_path,
        args=(
            "-p",
            "tests.helpers.home_snapshot",
            f"--home-leak-root={sentinel}",
            f"--home-leak-json={report}",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert list(data["leaking_tests"]) == ["test_probe.py::test_leaks"]
    leaked = data["leaking_tests"]["test_probe.py::test_leaks"]
    assert leaked["created"] == [".verdict/prove-at-rest", ".verdict/prove-at-rest/state.json"]
    assert "test_probe.py::test_leaks" in result.stdout
    assert "prove-at-rest/state.json" in result.stdout


def test_the_leak_detector_anchors_a_relative_root_and_report_before_a_test_changes_directory(
    tmp_path: Path,
) -> None:
    """A test that ``os.chdir``s must not move what the detector snapshots or where it reports."""
    work = tmp_path / "work"
    work.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sentinel = work / "sentinel-home"
    (sentinel / ".verdict").mkdir(parents=True)
    (sentinel / ".verdict" / "keep.json").write_text("keep")
    result = _child_pytest(
        work,
        {
            "test_probe.py": f"""
                import os, pathlib

                SENTINEL = pathlib.Path({str(sentinel)!r})

                def test_changes_directory_and_never_goes_back():
                    os.chdir({str(elsewhere)!r})

                def test_leaks_after_that():
                    (SENTINEL / ".verdict" / "state.json").write_text("{{}}")
            """
        },
        home=tmp_path,
        args=(
            "-p",
            "tests.helpers.home_snapshot",
            "--home-leak-root=sentinel-home",
            "--home-leak-json=leaks.json",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (elsewhere / "leaks.json").exists(), "the report followed the working directory"
    data = json.loads((work / "leaks.json").read_text())
    assert data["leaking_tests"] == {
        "test_probe.py::test_leaks_after_that": {
            "created": [".verdict/state.json"],
            "modified": [],
            "deleted": [],
        }
    }
    assert "keep.json" not in result.stdout, "the whole tree was reported, not the leak"
    assert data["root"] == str(sentinel.resolve())


def test_the_leak_detector_anchors_a_relative_report_path_before_the_session_ends_elsewhere(
    tmp_path: Path,
) -> None:
    """pytest restores the start directory before ``pytest_sessionfinish``; a hook can still undo it."""
    work = tmp_path / "work"
    work.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sentinel = work / "sentinel-home"
    (sentinel / ".verdict").mkdir(parents=True)
    result = _child_pytest(
        work,
        {
            "conftest.py": f"""
                import os
                import pytest

                @pytest.hookimpl(tryfirst=True)
                def pytest_sessionfinish(session):
                    os.chdir({str(elsewhere)!r})
            """,
            "test_probe.py": f"""
                import pathlib

                def test_leaks():
                    pathlib.Path({str(sentinel)!r}, ".verdict", "state.json").write_text("{{}}")
            """,
        },
        home=tmp_path,
        args=(
            "-p",
            "tests.helpers.home_snapshot",
            "--home-leak-root=sentinel-home",
            "--home-leak-json=leaks.json",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (elsewhere / "leaks.json").exists(), "the report followed the working directory"
    data = json.loads((work / "leaks.json").read_text())
    assert list(data["leaking_tests"]) == ["test_probe.py::test_leaks"]


def _detector_probe(sentinel: Path, marker: Path) -> dict[str, str]:
    return {
        "test_probe.py": f"""
            import pathlib

            SENTINEL = pathlib.Path({str(sentinel)!r})

            def test_leaks():
                pathlib.Path({str(marker)!r}).write_text("ran")
                (SENTINEL / ".verdict" / "state.json").write_text("{{}}")
        """
    }


@pytest.mark.parametrize(
    "parallel",
    [("-n", "2"), ("-n", "auto"), ("--dist", "load", "--tx", "popen//popen")],
    ids=["n2", "n-auto", "dist-tx"],
)
def test_the_leak_detector_refuses_to_run_under_xdist(
    tmp_path: Path, parallel: tuple[str, ...]
) -> None:
    sentinel = tmp_path / "sentinel-home"
    (sentinel / ".verdict").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "ran.marker"
    report = tmp_path / "leaks.json"
    result = _child_pytest(
        work,
        _detector_probe(sentinel, marker),
        home=tmp_path,
        args=(
            "-p",
            "tests.helpers.home_snapshot",
            f"--home-leak-root={sentinel}",
            f"--home-leak-json={report}",
            *parallel,
        ),
    )
    output = result.stdout + result.stderr
    assert result.returncode == int(pytest.ExitCode.USAGE_ERROR), output
    assert "home leak detector" in output and "serially" in output and "-n 0" in output, output
    assert not marker.exists(), "a test ran although the detector refused the run"
    assert not (sentinel / ".verdict" / "state.json").exists()
    assert not report.exists(), "a refused run wrote a report"


def test_the_leak_detector_still_runs_with_xdist_switched_off(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel-home"
    (sentinel / ".verdict").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "ran.marker"
    report = tmp_path / "leaks.json"
    result = _child_pytest(
        work,
        _detector_probe(sentinel, marker),
        home=tmp_path,
        args=(
            "-p",
            "tests.helpers.home_snapshot",
            f"--home-leak-root={sentinel}",
            f"--home-leak-json={report}",
            "-n",
            "0",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    data = json.loads(report.read_text())
    assert list(data["leaking_tests"]) == ["test_probe.py::test_leaks"]


def test_xdist_without_the_leak_detector_is_not_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    result = _child_pytest(
        work,
        {"test_ok.py": "def test_ok():\n    pass\n"},
        home=tmp_path,
        args=("-p", "tests.helpers.home_snapshot", "-n", "2"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
