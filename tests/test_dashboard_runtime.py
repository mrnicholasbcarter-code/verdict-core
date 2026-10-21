"""Dashboard runtime support does not change core Python 3.10 support."""

import runpy
import sys

import pytest

from verdict.actions import launch


def test_dashboard_launcher_rejects_python310(monkeypatch, capsys):
    monkeypatch.setattr(sys, "version_info", (3, 10, 20))
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
    assert launch.launch_dashboard() == 1
    assert (
        capsys.readouterr().err.strip()
        == "The dashboard needs Python 3.11 or newer (streamlit 1.66 and pillow 13 require it); core Verdict works on 3.10."
    )


def test_dashboard_module_rejects_python310_before_optional_imports(monkeypatch, capsys):
    monkeypatch.setattr(sys, "version_info", (3, 10, 20))
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("verdict.dashboard", run_name="__main__")
    assert exc.value.code == 1
    assert (
        capsys.readouterr().err.strip()
        == "The dashboard needs Python 3.11 or newer (streamlit 1.66 and pillow 13 require it); core Verdict works on 3.10."
    )
