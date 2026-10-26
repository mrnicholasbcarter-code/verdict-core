"""Behavioral regressions for scripts/quickstart.py route-probe truthfulness.

The route step is driven through a fake child process result. No network,
install, or live gateway is used. A probe passes only when the child exits 0,
the first stdout line is HTTP 200, and the remaining stdout is one JSON object
whose ``decision`` field is a non-empty string other than ``denied``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
QUICKSTART = ROOT / "scripts" / "quickstart.py"


def _load():
    spec = importlib.util.spec_from_file_location("quickstart_under_test", QUICKSTART)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "-c", "probe"], returncode=returncode, stdout=stdout, stderr=""
    )


def _route_output(status: int, body: object) -> str:
    return f"{status}\n{json.dumps(body)}\n"


@pytest.fixture
def quickstart(monkeypatch):
    module = _load()

    def fake_run(cmd, cwd=None, check=True, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        if " -c " in text or text.rstrip().endswith('-c "'):
            return module._probe_result
        return _completed("", returncode=0)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", lambda: _NullTemp())
    return module


class _NullTemp:
    def __enter__(self):
        return "/tmp/quickstart-probe-unused"

    def __exit__(self, *args):
        return False


def _run_main(module, stdout: str, *, returncode: int = 0) -> int | None:
    """Return the SystemExit code, or None when main returns without exiting."""
    module._probe_result = _completed(stdout, returncode=returncode)
    try:
        module.main()
    except SystemExit as caught:
        assert isinstance(caught.code, int)
        return caught.code
    return None


def test_valid_200_non_denied_decision_continues(quickstart, capsys) -> None:
    body = {"decision": "selected", "model": "example", "reason": "fit"}
    code = _run_main(quickstart, _route_output(200, body))
    assert code is None
    assert "RESULT: PASS" in capsys.readouterr().out


@pytest.mark.parametrize(
    "stdout",
    [
        _route_output(503, {"error": {"message": "server authentication is not configured"}}),
        _route_output(422, {"detail": [{"loc": ["body", "task"], "msg": "Field required"}]}),
        "200\nnot-json\n",
        _route_output(200, ["not", "an", "object"]),
        _route_output(200, {"model": "example"}),
        _route_output(200, {"decision": ""}),
        _route_output(200, {"decision": "denied", "reason": "managed intelligence unavailable"}),
        "503\n",
    ],
)
def test_unsuccessful_probe_exits_nonzero_and_does_not_pass(
    quickstart, capsys, stdout: str
) -> None:
    code = _run_main(quickstart, stdout)
    captured = capsys.readouterr().out
    assert code not in (None, 0)
    assert "RESULT: PASS" not in captured
    assert "QUICKSTART PASSED" not in captured


def test_child_probe_failure_is_nonzero(quickstart, capsys) -> None:
    code = _run_main(quickstart, "", returncode=1)
    captured = capsys.readouterr().out
    assert code not in (None, 0)
    assert "RESULT: PASS" not in captured
