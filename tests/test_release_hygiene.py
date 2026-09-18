"""BOD-113: spend evidence, packaged fixtures, and install extras are truthful."""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

from verdict.fixture_paths import FIXTURE_ROOTS, resolve_fixture_path, resolve_fixture_workspace

REPO = Path(__file__).resolve().parent.parent


def test_default_fixtures_resolve_from_a_foreign_cwd(tmp_path: Path, monkeypatch) -> None:
    """Documented benchmark commands must not depend on running from the checkout."""
    monkeypatch.chdir(tmp_path)
    for relative in (
        "benchmarks/fixtures/reproducible.json",
        "benchmarks/fixtures/legit_paired_savings.json",
        "benchmarks/fixtures/direct_vs_verdict.json",
    ):
        resolved = resolve_fixture_path(relative)
        assert resolved.is_file(), relative
        assert resolved.is_absolute()


def test_fixture_workspace_resolves_next_to_a_relocated_fixture(
    tmp_path: Path, monkeypatch
) -> None:
    """A wheel may live anywhere; the workspace is found beside the fixture file."""
    relocated = tmp_path / "site" / "verdict" / "data" / "benchmarks" / "fixtures"
    (relocated / "legit_workspace" / "docs").mkdir(parents=True)
    fixture = relocated / "legit_paired_savings.json"
    fixture.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    workspace = resolve_fixture_workspace(fixture, "benchmarks/fixtures/legit_workspace")
    assert workspace == (relocated / "legit_workspace").resolve()


def test_unknown_fixture_path_is_returned_unchanged() -> None:
    assert resolve_fixture_path("nope/missing.json") == Path("nope/missing.json")
    assert FIXTURE_ROOTS[0] == REPO


def test_wheel_force_includes_benchmark_fixtures() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    force = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force["benchmarks/fixtures"] == "verdict/data/benchmarks/fixtures"


def test_all_extra_is_a_superset_of_server_and_dashboard() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extras = config["project"]["optional-dependencies"]

    def names(items: list[str]) -> set[str]:
        return {re.split(r"[<>=!~\[ ]", item, maxsplit=1)[0].lower() for item in items}

    assert names(extras["server"]) | names(extras["dashboard"]) <= names(extras["all"])
    versions = config["project"]["classifiers"]
    assert "Programming Language :: Python :: 3.13" in versions


def test_dashboard_never_labels_hardcoded_prices_as_actual_cost() -> None:
    source = (REPO / "verdict" / "dashboard.py").read_text(encoding="utf-8")
    assert "Actual Cost" not in source
    assert "Cost without verdict" not in source
    # The observed-receipt path is the only thing that may be called measured.
    assert "observed_cost_usd" in source
    # Price assumptions exist only under an explicit synthetic label.
    assert "SYNTHETIC_PRICES_USD_PER_REQUEST" in source
    assert "NOT measured" in source


def test_ci_has_python_matrix_and_no_head_masked_smoke() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert f'"{version}"' in ci
    assert "verdict-client-example.ts" not in ci, "missing producer must not be invoked"
    assert "| head -30" not in ci, "piping through head masks the producer exit status"
    assert "npm run verify:package" in ci
    assert 'pip install ".[all]"' in ci
    assert "verdict benchmark --savings --output-json" in ci
