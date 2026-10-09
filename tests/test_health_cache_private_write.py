"""Private cache publication. All paths and HOME are isolated by pytest fixtures."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

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
    sentinel.chmod(0o400)
    stale = path.with_suffix(".json.tmp")
    stale.symlink_to(sentinel)
    _write(HealthCache(path), writer)
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o400
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
