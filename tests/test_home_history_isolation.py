# test_home_history_isolation
"""Prompt history honours VERDICT_HOME and only tightens unsafe directories."""

import tempfile
from pathlib import Path

import pytest

from verdict import home


def test_history_uses_verdict_home_over_home_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """VERDICT_HOME set -> history under VERDICT_HOME; nothing written under HOME."""
    with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_vh:
        home_path = Path(tmp_home)
        vh_path = Path(tmp_vh)

        monkeypatch.setenv("HOME", str(home_path))
        monkeypatch.setenv("VERDICT_HOME", str(vh_path))

        hist_file = home.history_file()
        assert hist_file == vh_path / "prompt_history"

        home._save_history(hist_file, ["test line"])

        assert (vh_path / "prompt_history").exists()
        assert not (home_path / ".verdict").exists()


def test_history_falls_back_to_home_verdict_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """VERDICT_HOME unset -> history under HOME/.verdict."""
    with tempfile.TemporaryDirectory() as tmp_home:
        home_path = Path(tmp_home)

        monkeypatch.setenv("HOME", str(home_path))
        monkeypatch.delenv("VERDICT_HOME", raising=False)

        hist_file = home.history_file()
        assert hist_file == home_path / ".verdict" / "prompt_history"

        home._save_history(hist_file, ["test line"])
        assert (home_path / ".verdict" / "prompt_history").exists()


def test_history_treats_empty_verdict_home_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty VERDICT_HOME behaves exactly like an unset one."""
    with tempfile.TemporaryDirectory() as tmp_home:
        home_path = Path(tmp_home)

        monkeypatch.setenv("HOME", str(home_path))
        monkeypatch.setenv("VERDICT_HOME", "")

        assert home.history_file() == home_path / ".verdict" / "prompt_history"


def test_history_permissions() -> None:
    """A 0755 dir is left at 0755; a 0777 dir is tightened to 0700."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)

        # 0755 stays 0755 (chmod explicitly: the process umask is 0002).
        vh_755 = base / "vh_755"
        vh_755.mkdir(mode=0o755)
        vh_755.chmod(0o755)

        home._save_history(vh_755 / "prompt_history", ["test line"])
        assert vh_755.stat().st_mode & 0o777 == 0o755

        # 0777 is tightened to 0700.
        vh_777 = base / "vh_777"
        vh_777.mkdir(mode=0o777)
        vh_777.chmod(0o777)

        home._save_history(vh_777 / "prompt_history", ["test line"])
        assert vh_777.stat().st_mode & 0o777 == 0o700
