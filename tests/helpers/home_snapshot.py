"""Leak detector: which tests write into a home tree they should never touch.

Used to prove (and, later, to re-check) that no test reaches the operator's real
``~/.verdict``. It snapshots a directory tree (path, kind, size, mtime, sha256 of
regular files; symlinks are recorded, never followed) and diffs two snapshots.
File contents are hashed, never read into output.

Per-test attribution runs as a pytest plugin::

    mkdir -p /tmp/iso-home/.verdict
    env HOME=/tmp/iso-home VERDICT_HOME=/tmp/iso-home/.verdict \\
        uv run --frozen python -m pytest -p tests.helpers.home_snapshot \\
        --home-leak-root=/tmp/iso-home tests/test_orch_eligibility.py

The root is a THROWAWAY home that the process starts with as ``HOME``. The
autouse fixtures in ``tests/conftest.py`` re-point ``HOME`` at a per-test temp
directory, so a clean test leaves the throwaway home untouched; every path the
report lists was written by something that bypassed the fixtures (import-time or
wider-scope fixture code, a hardcoded path, a subprocess with its own env). The
plugin snapshots around each test, so run serially (no ``-n``): under xdist the
workers share the root and attribution is ambiguous.

Use a throwaway root, never the operator's real home: the real home is changed
by other processes (installed services) and would blame tests for their writes.
``tests/helpers/home_guard.py`` is the enforcement that works against the real
home.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_CHUNK = 1 << 20


@dataclass(frozen=True)
class Entry:
    """One path in a snapshot. ``digest`` is the sha256 of a regular file."""

    kind: str  # "file" | "dir" | "symlink" | "other"
    size: int
    mtime_ns: int
    digest: str | None

    def describe(self) -> str:
        return f"{self.kind} {self.size}B"


@dataclass(frozen=True)
class Diff:
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.created or self.modified or self.deleted)

    def paths(self) -> tuple[str, ...]:
        return self.created + self.modified + self.deleted

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "created": list(self.created),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
        }


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _walk(root: Path) -> Iterator[Path]:
    """Every path under ``root`` without following symlinks."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = list(os.scandir(current))
        except OSError:
            continue
        for child in children:
            path = Path(child.path)
            yield path
            if child.is_dir(follow_symlinks=False):
                stack.append(path)


def snapshot(root: Path) -> dict[str, Entry]:
    """Map every path under ``root`` (posix, relative) to its :class:`Entry`.

    Nothing is skipped. A missing root is an empty snapshot.
    """
    entries: dict[str, Entry] = {}
    for path in _walk(root):
        try:
            stat = path.lstat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[rel] = Entry("symlink", stat.st_size, stat.st_mtime_ns, None)
        elif path.is_dir():
            entries[rel] = Entry("dir", 0, stat.st_mtime_ns, None)
        elif path.is_file():
            entries[rel] = Entry("file", stat.st_size, stat.st_mtime_ns, _sha256(path))
        else:
            entries[rel] = Entry("other", stat.st_size, stat.st_mtime_ns, None)
    return entries


def diff(before: dict[str, Entry], after: dict[str, Entry]) -> Diff:
    """Created, modified (content, size, kind or mtime) and deleted paths."""
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        rel
        for rel in set(before) & set(after)
        # A directory's mtime moves whenever a child is added; the child is
        # reported on its own, so only a kind change counts for directories.
        if (
            before[rel].kind != after[rel].kind
            or (
                before[rel].kind != "dir"
                and (
                    before[rel].digest != after[rel].digest
                    or before[rel].size != after[rel].size
                    or before[rel].mtime_ns != after[rel].mtime_ns
                )
            )
        )
    )
    return Diff(tuple(created), tuple(modified), tuple(deleted))


# ---------------------------------------------------------------------------
# pytest plugin: per-test attribution against a throwaway home
# ---------------------------------------------------------------------------

COLLECTION_PHASE = "<collection-and-session-setup>"


@dataclass
class LeakReport:
    root: Path
    by_test: dict[str, Diff] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "leaking_tests": {node: found.as_dict() for node, found in self.by_test.items()},
        }

    def render(self) -> list[str]:
        lines: list[str] = []
        for node, found in self.by_test.items():
            lines.append(f"{node}")
            for label, paths in (
                ("created", found.created),
                ("modified", found.modified),
                ("deleted", found.deleted),
            ):
                lines.extend(f"    {label}: {rel}" for rel in paths)
        return lines


class HomeLeakDetector:
    """Pytest plugin: snapshot ``root`` around every test and keep the diffs."""

    def __init__(self, root: Path, json_path: Path | None = None) -> None:
        self.report = LeakReport(root=root)
        self._json_path = json_path
        # Baseline at plugin start: whatever is already there is not a leak.
        self._last: dict[str, Entry] = snapshot(root)

    def _record(self, node: str, before: dict[str, Entry], after: dict[str, Entry]) -> None:
        found = diff(before, after)
        if found:
            self.report.by_test[node] = found

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        # Anything between plugin start and the first test: imports, conftest,
        # session-scoped fixtures run later and are attributed to the first test.
        now = snapshot(self.report.root)
        self._record(COLLECTION_PHASE, self._last, now)
        self._last = now

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Any:
        before = snapshot(self.report.root)
        # Writes between tests (module-scope teardown) belong to the previous
        # test's neighbour, not to this one: attribute them to the gap.
        self._record(f"<between tests, before {item.nodeid}>", self._last, before)
        outcome = yield
        after = snapshot(self.report.root)
        self._record(item.nodeid, before, after)
        self._last = after
        del outcome

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        final = snapshot(self.report.root)
        self._record("<session teardown>", self._last, final)
        if self._json_path is not None:
            self._json_path.write_text(json.dumps(self.report.as_dict(), indent=2) + "\n")

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        terminalreporter.section(f"home leak detector ({self.report.root})")
        if not self.report.by_test:
            terminalreporter.write_line("no test wrote under the throwaway home")
            return
        terminalreporter.write_line(
            f"{len(self.report.by_test)} test(s) wrote under the throwaway home:"
        )
        for line in self.report.render():
            terminalreporter.write_line(line)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("home-leak")
    group.addoption(
        "--home-leak-root",
        default=None,
        help="Throwaway home to snapshot around every test (leak detector).",
    )
    group.addoption(
        "--home-leak-json", default=None, help="Write the leak report as JSON to this path."
    )


def pytest_configure(config: pytest.Config) -> None:
    root = config.getoption("--home-leak-root")
    if not root:
        return
    json_path = config.getoption("--home-leak-json")
    config.pluginmanager.register(
        HomeLeakDetector(Path(root), Path(json_path) if json_path else None), "home-leak-detector"
    )
