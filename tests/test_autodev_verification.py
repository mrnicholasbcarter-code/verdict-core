from pathlib import Path
from typing import Any

from verdict.autodev_run import _verify
from verdict.work_unit import WorkUnit


def test_missing_verification_never_runs_or_passes(tmp_path: Path) -> None:
    unit = WorkUnit("one", "change", ("a.py",), ("true",))
    object.__setattr__(unit, "verification_command", ())
    calls: list[Any] = []

    def runner(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("missing command must not run")

    verified, reason = _verify(unit, tmp_path, (), runner=runner)
    assert not verified
    assert "missing verification command" in reason
    assert calls == []


def test_failing_verification_is_unverified(tmp_path: Path) -> None:
    import subprocess
    import sys

    unit = WorkUnit("one", "change", ("a.py",), (sys.executable, "-c", "raise SystemExit(7)"))
    verified, reason = _verify(unit, tmp_path, (), runner=subprocess.run)
    assert not verified
    assert "verification exited 7" in reason


def test_verification_command_resolves_to_repo_venv(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_py = venv_bin / "python"
    venv_py.touch()

    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    unit1 = WorkUnit("u1", "change", ("a.py",), ("python", "-m", "pytest", "tests/test_foo.py"))
    ok, _ = _verify(unit1, tmp_path, (), runner=fake_runner)
    assert ok
    assert calls[-1] == [str(venv_py), "-m", "pytest", "tests/test_foo.py"]

    unit2 = WorkUnit("u2", "change", ("a.py",), ("python3", "-m", "pytest", "tests/test_foo.py"))
    ok, _ = _verify(unit2, tmp_path, (), runner=fake_runner)
    assert ok
    assert calls[-1] == [str(venv_py), "-m", "pytest", "tests/test_foo.py"]

    unit3 = WorkUnit("u3", "change", ("a.py",), ("pytest", "tests/test_foo.py"))
    ok, _ = _verify(unit3, tmp_path, (), runner=fake_runner)
    assert ok
    assert calls[-1] == [str(venv_py), "-m", "pytest", "tests/test_foo.py"]


def test_verification_command_falls_back_to_uv_run_without_venv(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    unit1 = WorkUnit("u1", "change", ("a.py",), ("python", "-m", "pytest", "tests/test_foo.py"))
    ok, _ = _verify(unit1, tmp_path, (), runner=fake_runner)
    assert ok
    assert calls[-1] == ["uv", "run", "python", "-m", "pytest", "tests/test_foo.py"]

    unit2 = WorkUnit("u2", "change", ("a.py",), ("pytest", "tests/test_foo.py"))
    ok, _ = _verify(unit2, tmp_path, (), runner=fake_runner)
    assert ok
    assert calls[-1] == ["uv", "run", "pytest", "tests/test_foo.py"]

    unit3 = WorkUnit("u3", "change", ("a.py",), ("git", "status"))
    ok, _ = _verify(unit3, tmp_path, (), runner=fake_runner)
    assert ok
    assert calls[-1] == ["git", "status"]
