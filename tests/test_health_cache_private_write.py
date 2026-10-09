"""Private cache publication. All paths and HOME are isolated by pytest fixtures."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from verdict.orchestration import health_cache
from verdict.orchestration.health_cache import HealthCache, HealthCacheError


@pytest.fixture(params=["save", "merge_and_save"])
def writer(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _write(cache: HealthCache, writer: str) -> None:
    if writer == "save":
        cache.save()
    else:
        cache.merge_and_save(lambda _: None)


@pytest.mark.parametrize("existing_mode", [None, 0o600, 0o400, 0o664, 0o444])
@pytest.mark.parametrize("umask", [0o000, 0o002])
def test_private_mode_survives_repeated_publication(
    tmp_path: Path, writer: str, existing_mode: int | None, umask: int
) -> None:
    path = tmp_path / "cache.json"
    if existing_mode is not None:
        path.write_text("{}\n", encoding="utf-8")
        path.chmod(existing_mode)
    cache = HealthCache(path)
    previous_umask = os.umask(umask)
    try:
        for _ in range(2):
            _write(cache, writer)
            expected = 0o600 if existing_mode is None else existing_mode & 0o600
            assert stat.S_IMODE(path.stat().st_mode) == expected
    finally:
        os.umask(previous_umask)


def test_fixed_tmp_symlink_is_unrelated(tmp_path: Path, writer: str) -> None:
    path = tmp_path / "cache.json"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("do not touch", encoding="utf-8")
    sentinel.chmod(0o600)
    stale = path.with_suffix(".json.tmp")
    stale.symlink_to(sentinel)
    _write(HealthCache(path), writer)
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o600
    assert stale.is_symlink()


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
def test_non_regular_destination_fails_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str, kind: str
) -> None:
    path = tmp_path / "cache.json"
    cache = HealthCache(path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("{}\n", encoding="utf-8")
    sentinel.chmod(0o400)
    if kind in {"symlink", "dangling"}:
        path.symlink_to(sentinel if kind == "symlink" else tmp_path / "missing")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    real_read = Path.read_text

    def guarded_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            pytest.fail("non-regular cache must not be opened")
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", guarded_read)
    with pytest.raises(HealthCacheError, match="regular file"):
        _write(cache, writer)
    assert sentinel.read_text(encoding="utf-8") == "{}\n"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o400
    assert path.lstat()


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
def test_constructor_rejects_non_regular_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "cache.json"
    if kind in {"symlink", "dangling"}:
        sentinel = tmp_path / "sentinel"
        if kind == "symlink":
            sentinel.write_text("{}\n", encoding="utf-8")
        path.symlink_to(sentinel)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    if kind == "fifo":
        monkeypatch.setattr(
            Path, "read_text", lambda *a, **k: pytest.fail("FIFO must not be opened")
        )
    with pytest.raises(HealthCacheError, match="regular file"):
        HealthCache(path)


@pytest.mark.parametrize("failure", ["fchmod", "fdopen", "write", "flush", "fsync", "replace"])
def test_prepublication_failure_keeps_authoritative_bytes_and_cleans_own_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str, failure: str
) -> None:
    path = tmp_path / "cache.json"
    path.write_text('{"cursor": {"keep": true}}\n', encoding="utf-8")
    path.chmod(0o400)
    before = path.read_bytes()
    stale = tmp_path / "cache.json.tmp"
    stale.write_text("unrelated", encoding="utf-8")
    before_names = set(tmp_path.iterdir()) | {path.with_suffix(".json.lock")}
    cache = HealthCache(path)
    fds: list[int] = []
    real_mkstemp = health_cache.tempfile.mkstemp

    def mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
        fds.append(fd)
        assert Path(name).parent == path.parent
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        return fd, name

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(f"injected {failure}")

    class BrokenStream(io.StringIO):
        def write(self, text: str) -> int:
            if failure == "write":
                fail()
            return super().write(text)

        def flush(self) -> None:
            if failure == "flush":
                fail()
            super().flush()

    monkeypatch.setattr(health_cache.tempfile, "mkstemp", mkstemp)
    if failure in {"write", "flush"}:
        monkeypatch.setattr(health_cache.os, "fdopen", lambda *a, **k: BrokenStream())
    else:
        monkeypatch.setattr(health_cache.os, failure, fail)
    with pytest.raises(OSError, match=f"injected {failure}"):
        _write(cache, writer)
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert set(tmp_path.iterdir()) == before_names
    assert stale.read_text(encoding="utf-8") == "unrelated"
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_fchmod_file_sync_replace_directory_sync_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    path = tmp_path / "cache.json"
    path.write_text('{"cursor": {"unicode": "caf\u00e9"}}', encoding="utf-8")
    path.chmod(0o400)
    cache = HealthCache(path)
    events: list[str] = []
    real_chmod, real_sync, real_replace = os.fchmod, os.fsync, os.replace

    def fchmod(fd: int, mode: int) -> None:
        assert os.fstat(fd).st_size == 0  # restrictive mode precedes all bytes
        real_chmod(fd, mode)
        events.append("fchmod")

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("directory")
            assert cache.path.exists()
        else:
            events.append("file")
            assert os.fstat(fd).st_size > 0  # buffered write was flushed
            assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o400
        real_sync(fd)

    def replace(source: str | Path, target: str | Path) -> None:
        assert Path(source).parent == path.parent
        assert Path(source) != path.with_suffix(".json.tmp")
        assert Path(target) == path
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(health_cache.os, "fchmod", fchmod)
    monkeypatch.setattr(health_cache.os, "fsync", fsync)
    monkeypatch.setattr(health_cache.os, "replace", replace)
    _write(cache, writer)
    assert events == ["fchmod", "file", "replace", "directory"]
    assert not list(tmp_path.glob(".*.tmp"))
