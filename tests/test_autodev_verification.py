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
