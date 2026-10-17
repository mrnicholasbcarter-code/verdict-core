"""Doctor's service code-path check (BOD-421): a tracked unit must import the
``verdict`` package its interpreter is configured for, not one shadowed by
the unit's WorkingDirectory.

The historical failure: ``verdict-prime-sync.service`` ran with a stale
checkout as WorkingDirectory, so ``python -m verdict`` imported that
checkout's ``verdict`` package (cwd precedes the editable install on
``sys.path``) and the timer silently ran pre-BOD-420 code. The check resolves
the package twice -- once from a neutral cwd (what the interpreter's venv is
configured to import, i.e. the tracked repo checkout) and once from the unit's
WorkingDirectory (what the service actually imports) -- and fails when they
differ. Tests use the real interpreter with a real shadowing layout, not a
mocked resolver, so the negative proof exercises the same mechanism as the
incident.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from verdict import doctor_diagnostics as dd

REPO_ROOT = Path(dd.__file__).resolve().parent.parent
CONFIGURED_PACKAGE = Path(dd.__file__).resolve().parent
PRIME_SYNC = "verdict-prime-sync.service"


def _fake_unit_props(
    *, working_directory: str, interpreter: str = sys.executable, load_state: str = "loaded"
) -> Any:
    """A ``systemctl --user show`` stand-in shaped like systemd's real output."""

    def props(unit: str) -> dict[str, str]:
        assert unit == PRIME_SYNC
        return {
            "LoadState": load_state,
            "WorkingDirectory": working_directory,
            "ExecStart": (
                f"{{ path={interpreter} ; argv[]={interpreter} -m verdict harness prime sync-models"
                " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ;"
                " code=(null) ; status=0/0 }"
            ),
        }

    return props


# ---------------------------------------------------------------------------
# the negative proof: a WorkingDirectory verdict/ package shadows the
# interpreter's configured (editable) install, exactly like the incident
# ---------------------------------------------------------------------------


def test_prime_sync_row_fails_when_working_directory_shadows_configured_package(
    tmp_path: Path,
) -> None:
    shadow = tmp_path / "stale-checkout"
    (shadow / "verdict").mkdir(parents=True)
    (shadow / "verdict" / "__init__.py").write_text("")

    rows = dd.service_code_path_rows(unit_props=_fake_unit_props(working_directory=str(shadow)))

    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == "Prime-sync code path"
    assert row["status"] == "failed"
    assert PRIME_SYNC in row["detail"]
    assert str(shadow) in row["detail"]
    assert str(shadow / "verdict") in row["detail"]
    assert str(CONFIGURED_PACKAGE) in row["detail"]


def test_prime_sync_row_ok_when_working_directory_matches_configured_package() -> None:
    rows = dd.service_code_path_rows(unit_props=_fake_unit_props(working_directory=str(REPO_ROOT)))

    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert PRIME_SYNC in row["detail"]
    assert str(REPO_ROOT) in row["detail"]
    assert str(CONFIGURED_PACKAGE) in row["detail"]


def test_prime_sync_row_skipped_when_unit_not_loaded() -> None:
    rows = dd.service_code_path_rows(
        unit_props=_fake_unit_props(working_directory=str(REPO_ROOT), load_state="not-found")
    )

    assert rows == []


def test_prime_sync_row_fails_when_interpreter_cannot_import_verdict(tmp_path: Path) -> None:
    rows = dd.service_code_path_rows(
        unit_props=_fake_unit_props(
            working_directory=str(REPO_ROOT), interpreter="/nonexistent/verdict-python"
        )
    )

    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "could not resolve" in rows[0]["detail"]


def test_prime_sync_row_fails_when_working_directory_is_missing() -> None:
    rows = dd.service_code_path_rows(
        unit_props=lambda unit: {
            "LoadState": "loaded",
            "WorkingDirectory": "",
            "ExecStart": f"{{ path={sys.executable} ; argv[]={sys.executable} -m verdict }}",
        }
    )

    assert len(rows) == 1
    assert rows[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_parse_execstart_interpreter_reads_systemd_show_format() -> None:
    value = (
        "{ path=/home/x/repo/.venv/bin/python ; argv[]=/home/x/repo/.venv/bin/python -m verdict"
        " ; ignore_errors=no ; status=0/0 }"
    )
    assert dd._parse_execstart_interpreter(value) == "/home/x/repo/.venv/bin/python"


def test_parse_execstart_interpreter_reads_plain_path_form() -> None:
    assert dd._parse_execstart_interpreter("/usr/bin/python -m verdict") == "/usr/bin/python"


def test_parse_execstart_interpreter_returns_none_for_empty() -> None:
    assert dd._parse_execstart_interpreter("") is None
    assert dd._parse_execstart_interpreter("   ") is None
