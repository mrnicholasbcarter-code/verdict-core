"""No test can write the operator's real Verdict home (issue #1306).

Three layers are pinned here:

* resolution: under the per-test isolation every path ``verdict.paths`` (and the
  other state resolvers) derives lands under the per-test home, and every path
  env var ``verdict/`` reads is cleared or accounted for;
* enforcement: ``tests/helpers/home_guard.py`` refuses and reports any write to
  the real state roots. It is proved against a FAKE "real home" (never the
  operator's), including end to end in a child pytest run with a deliberately
  leaking test;
* detection: ``tests/helpers/home_snapshot.py`` snapshots a throwaway home so a
  run can list exactly which tests wrote into it.
"""

from __future__ import annotations

import contextlib
import errno
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
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
        ("open", ("{o}/../{rn}/x.json", "w", _flags("O_WRONLY", "O_CREAT")), True),
        ("os.rename", ("{o}/a", "{r}/b", None, None), True),
        ("os.rename", ("{r}/a", "{o}/b", None, None), True),
        ("os.rename", ("{o}/a", "{o}/b", None, None), False),
        ("os.remove", ("{r}/model-metadata.json", None), True),
        ("os.rmdir", ("{r}/dir", None), True),
        ("os.mkdir", ("{r}/dir", 0o777, None), True),
        ("os.truncate", ("{r}/state.json", 0), True),
        ("os.symlink", ("{o}/target", "{r}/link", None), True),
        ("os.symlink", ("{r}/target", "{o}/link", None), False),
        ("shutil.copyfile", ("{o}/src", "{r}/dst"), True),
        ("shutil.copyfile", ("{r}/src", "{o}/dst"), False),
        ("shutil.move", ("{r}/src", "{o}/dst"), True),
        ("shutil.rmtree", ("{r}", None), True),
        ("sqlite3.connect", ("{r}/receipts.db",), True),
        ("sqlite3.connect", (":memory:",), False),
        ("sqlite3.connect", ("file:{r}/receipts.db",), True),
        ("sqlite3.connect", ("file://{r}/receipts.db?mode=rwc",), True),
        ("sqlite3.connect", ("file:{r}/receipts.db?mode=ro",), False),
        ("sqlite3.connect", ("file:{o}/receipts.db",), False),
        ("sqlite3.connect", ("file:{o}/x?cache=shared",), False),
        ("sqlite3.connect", ("file::memory:?cache=shared",), False),
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
        return value.format(r=root, o=other, rn=root.relative_to(tmp_path))

    filled = tuple(fill(a) for a in args)
    if denied:
        with pytest.raises(PermissionError) as raised:
            guard.check(event, filled)
        assert raised.value.errno == errno.EACCES
        assert len(guard.violations) == 1 and guard.violations[0].event == event
    else:
        guard.check(event, filled)
        assert guard.violations == []


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


def test_a_leaking_test_fails_the_run_and_names_the_file(tmp_path: Path) -> None:
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


def test_a_clean_run_passes_with_the_guard_loaded(tmp_path: Path) -> None:
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
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_write_after_the_last_test_still_fails_the_run(tmp_path: Path) -> None:
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
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "late.json" in output
    assert not (fake_home / ".verdict").exists()


def test_exported_state_env_is_cleared_and_still_protected(tmp_path: Path) -> None:
    """The real conftest, with the operator's state env exported, in a child run."""
    fake_home = tmp_path / "fake-real-home"
    fake_home.mkdir()
    exported = tmp_path / "exported-state"
    exported.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(REPO_ROOT / "tests" / "conftest.py", work / "conftest.py")
    polluted = {name: str(exported / name.lower()) for name in home_guard.HOST_STATE_ENV}
    polluted["VERDICT_HOME"] = str(exported / "verdict-home")
    result = _child_pytest(
        work,
        {
            "test_probe.py": f"""
                import os, pathlib
                from tests.helpers.home_guard import GUARD, HOST_STATE_ENV

                EXPORTED = pathlib.Path({str(exported)!r})

                def test_no_exported_state_env_reaches_the_test():
                    assert [n for n in HOST_STATE_ENV if n in os.environ] == []

                def test_default_paths_land_under_the_per_test_home():
                    from verdict.paths import verdict_home
                    assert str(verdict_home()).startswith(os.environ["HOME"])

                def test_the_exported_location_is_still_refused():
                    assert str(EXPORTED / "verdict-home") in GUARD.roots
                    try:
                        (EXPORTED / "verdict-home" / "x.json").write_text("x")
                    except PermissionError:
                        GUARD.drain()
                    else:
                        raise AssertionError("write to the exported real location was allowed")
            """
        },
        home=fake_home,
        extra_env=polluted,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
    assert list(exported.iterdir()) == []


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
