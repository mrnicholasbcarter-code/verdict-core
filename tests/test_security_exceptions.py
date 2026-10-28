"""Tests for scripts/security_exceptions.py (BOD-342 Option B: scoped pillow exception).

Proves the exception machinery cannot silently widen: an unlisted CVE still fails,
an expired exception stops applying, and a listed ID scoped to one package/version
does not leak to a different package or a different version of the same package.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from packaging.version import Version

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import security_exceptions as sx


def _write_exceptions(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "security_exceptions.json"
    path.write_text(json.dumps({"schema_version": 1, "exceptions": entries}))
    return path


def _write_lock(tmp_path: Path, packages: list[tuple[str, str]]) -> Path:
    path = tmp_path / "uv.lock"
    body = "\n".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n' for name, version in packages
    )
    path.write_text(body)
    return path


ACTIVE_ENTRY = {
    "id": "CVE-2026-112895",
    "package": "pillow",
    "version": "12.3.0",
    "reason": "transitive via streamlit<13 in the optional dashboard extra",
    "expires": "2026-12-31",
    "removal_trigger": "bump pillow when streamlit allows pillow>=13",
}


def test_fixed_pillow_has_no_security_exceptions() -> None:
    """Every locked Pillow is fixed and no longer needs an advisory exception."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    root = Path(__file__).parent.parent
    with (root / "uv.lock").open("rb") as lock_file:
        packages = tomllib.load(lock_file)["package"]
    pillow_versions = [p["version"] for p in packages if p["name"] == "pillow"]
    assert pillow_versions
    assert all(int(version.split(".")[0]) >= 13 for version in pillow_versions)
    assert not any(exc.package == "pillow" for exc in sx.load_exceptions())


def test_all_locked_dnspython_versions_are_fixed() -> None:
    """Audit every Python resolution, including versions absent from this environment."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    root = Path(__file__).parent.parent
    with (root / "uv.lock").open("rb") as lock_file:
        packages = tomllib.load(lock_file)["package"]
    dns_versions = [p["version"] for p in packages if p["name"] == "dnspython"]
    assert dns_versions
    assert all(Version(version) >= Version("2.9.0") for version in dns_versions)
    assert not any(exc.package == "dnspython" for exc in sx.load_exceptions())


def test_unlisted_vulnerability_is_never_ignored(tmp_path: Path) -> None:
    """An unlisted pillow CVE is not in the active ignore list; certify_release's
    security step would still see and fail on it."""
    exceptions_path = _write_exceptions(tmp_path, [ACTIVE_ENTRY])
    lock_path = _write_lock(tmp_path, [("pillow", "12.3.0")])

    ids = sx.active_ignore_ids(
        sx.load_exceptions(exceptions_path), today=date(2026, 10, 21), lock_path=lock_path
    )

    assert ids == ["CVE-2026-112895"]
    assert "CVE-2026-999999" not in ids  # some other, unlisted pillow CVE


def test_exception_ignored_before_expiry_and_not_after(tmp_path: Path) -> None:
    """The same exception applies before its expiry date and stops applying after."""
    exceptions_path = _write_exceptions(tmp_path, [ACTIVE_ENTRY])
    lock_path = _write_lock(tmp_path, [("pillow", "12.3.0")])
    exceptions = sx.load_exceptions(exceptions_path)

    before = sx.active_ignore_ids(exceptions, today=date(2026, 12, 31), lock_path=lock_path)
    after = sx.active_ignore_ids(exceptions, today=date(2027, 1, 1), lock_path=lock_path)

    assert before == ["CVE-2026-112895"]
    assert after == []


def test_listed_id_on_different_package_is_not_ignored(tmp_path: Path) -> None:
    """An exception scoped to pillow@12.3.0 must not exempt the same ID for another
    package, or for pillow at a different version."""
    exceptions_path = _write_exceptions(tmp_path, [ACTIVE_ENTRY])

    other_package_lock = _write_lock(tmp_path, [("notpillow", "12.3.0")])
    ids_other_package = sx.active_ignore_ids(
        sx.load_exceptions(exceptions_path), today=date(2026, 10, 21), lock_path=other_package_lock
    )
    assert ids_other_package == []

    other_version_lock = _write_lock(tmp_path, [("pillow", "13.0.0")])
    ids_other_version = sx.active_ignore_ids(
        sx.load_exceptions(exceptions_path), today=date(2026, 10, 21), lock_path=other_version_lock
    )
    assert ids_other_version == []


def test_build_pip_audit_ignore_flags_shape(tmp_path: Path) -> None:
    """Flags are well-formed `--ignore-vuln <ID>` pairs, one per active exception."""
    exceptions_path = _write_exceptions(tmp_path, [ACTIVE_ENTRY])
    lock_path = _write_lock(tmp_path, [("pillow", "12.3.0")])

    flags = sx.build_pip_audit_ignore_flags(
        today=date(2026, 10, 21), exceptions_path=exceptions_path, lock_path=lock_path
    )

    assert flags == ["--ignore-vuln", "CVE-2026-112895"]


def test_osv_scanner_toml_is_generated_from_same_source() -> None:
    """scripts/generate_osv_exceptions.py --check proves osv-scanner.toml did not drift
    from config/security_exceptions.json (the single source of truth)."""
    import subprocess

    root = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "generate_osv_exceptions.py"), "--check"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
