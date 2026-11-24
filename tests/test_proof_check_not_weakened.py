"""Tests for scripts/proof/check_not_weakened.py (issue #1307, design D9).

Every case builds a throwaway git repository: a base commit on ``main``, then a
branch that changes it. The checker is run against ``main`` and must exit 0 (nothing
weakened), 1 (a weakening, every finding printed) or 2 (git, usage or parse failure).
The toy test sources below are strings: the checker finds code by parsing, so words
that only appear as data must not be flagged.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.proof.check_not_weakened import GIT_LOCAL_ENV, main

CLEAN_TEST = "import pytest\n\n\ndef test_a():\n    assert True\n"
PYPROJECT = (
    '[project]\nname = "toy"\n\n[tool.pytest.ini_options]\naddopts = "-v --tb=short"\n'
    'markers = ["timing: serial"]\n\n[tool.ruff]\nline-length = 100\n'
)
BASE_CONTRACT: dict[str, Any] = {
    "schema_version": 1,
    "name": "toy",
    "runner": "native",
    "modes": {
        "targeted": {"phases": ["format", "unit_targeted"]},
        "full": {"phases": ["format", "unit_targeted", "unit_full", "security", "adversarial"]},
    },
    "gates": [
        {"id": "format", "phase": "format", "classify": "format", "command": ["ruff", "format"]},
        {
            "id": "unit_targeted",
            "phase": "unit_targeted",
            "classify": "test",
            "command": ["pytest", "{targets}", "-q"],
            "targets_placeholder": "{targets}",
        },
        {
            "id": "unit_full",
            "phase": "unit_full",
            "classify": "test",
            "command": ["pytest", "tests/", "-q", "--tb=short"],
        },
        {
            "id": "security_bandit",
            "phase": "security",
            "classify": "security",
            "command": ["bandit", "-r", "verdict"],
            "required": False,
        },
        {
            "id": "promptfoo",
            "phase": "adversarial",
            "classify": "adversarial",
            "command": ["promptfoo", "eval"],
            "when": "llm_boundary",
            "required": False,
        },
    ],
}


def clean_git_env() -> dict[str, str]:
    """The environment for a throwaway repository.

    A git hook (the push gate runs these tests from one) exports GIT_DIR and friends. Left in
    place they override ``cwd=`` and ``git -C``, so ``git init`` and ``git commit`` would act
    on the real repository. Every GIT_* variable is removed, then the ones wanted are set.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
    )
    return env


def _contract_text(contract: dict[str, Any]) -> str:
    return yaml.safe_dump(contract, sort_keys=False)


def _edited(edit: Callable[[dict[str, Any]], None]) -> str:
    contract: dict[str, Any] = yaml.safe_load(_contract_text(BASE_CONTRACT))
    edit(contract)
    return _contract_text(contract)


def _gate(contract: dict[str, Any], gate_id: str) -> dict[str, Any]:
    return next(g for g in contract["gates"] if g["id"] == gate_id)


class Repo:
    """A throwaway repository with a base commit on ``main`` and a ``feature`` branch."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.env = clean_git_env()
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.path, env=self.env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def apply(self, files: dict[str, str | bytes | None]) -> None:
        for rel, content in files.items():
            target = self.path / rel
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")

    def commit(self, files: dict[str, str | bytes | None], message: str = "change") -> str:
        self.apply(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


def base_files() -> dict[str, str | bytes | None]:
    return {
        "tests/test_a.py": CLEAN_TEST,
        "proof/contract.yaml": _contract_text(BASE_CONTRACT),
        "pyproject.toml": PYPROJECT,
        ".test_durations": "{}\n",
    }


def scenario(
    tmp_path: Path,
    head: dict[str, str | bytes | None],
    *,
    base: dict[str, str | bytes | None] | None = None,
) -> Repo:
    repo = Repo(tmp_path / "repo")
    repo.commit(base if base is not None else base_files(), "base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit(head, "head")
    return repo


def check(repo: Repo, capsys: pytest.CaptureFixture[str], base: str = "main") -> tuple[int, str]:
    code = main(["--base", base, "--repo", str(repo.path)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


# --- constructs that weaken a test: each is a module source that holds one ---

CONSTRUCTS: dict[str, str] = {
    "skip decorator": "@pytest.mark.skip(reason='r')\ndef test_a():\n    assert True\n",
    "skipif decorator": "@pytest.mark.skipif(True, reason='r')\ndef test_a():\n    assert True\n",
    "bare xfail": "@pytest.mark.xfail\ndef test_a():\n    assert True\n",
    "xfail strict": "@pytest.mark.xfail(strict=True)\ndef test_a():\n    assert True\n",
    "skip call": "def test_a():\n    pytest.skip('x')\n",
    "importorskip call": "def test_a():\n    pytest.importorskip('yaml')\n",
    "timing marker": "@pytest.mark.timing\ndef test_a():\n    assert True\n",
    "integration marker": "@pytest.mark.integration\ndef test_a():\n    assert True\n",
    "usefixtures marker": "@pytest.mark.usefixtures('x')\ndef test_a():\n    assert True\n",
    "pytestmark": "pytestmark = pytest.mark.usefixtures('x')\n\ndef test_a():\n    assert True\n",
    "empty pytestmark": "pytestmark = []\n\ndef test_a():\n    assert True\n",
    "collect_ignore": "collect_ignore = ['x.py']\n\ndef test_a():\n    assert True\n",
    "collect_ignore_glob": "collect_ignore_glob = ['*.py']\n\ndef test_a():\n    assert True\n",
    "pytest_deselected hook": "def pytest_deselected(items):\n    pass\n\ndef test_a():\n    assert True\n",
    "pytest_deselected call": "def test_a(config):\n    config.hook.pytest_deselected(items=[])\n",
    "modifyitems hook": "def pytest_collection_modifyitems(items):\n    items[:] = []\n\ndef test_a():\n    assert True\n",
    "ignore_collect hook": "def pytest_ignore_collect(collection_path):\n    return True\n\ndef test_a():\n    assert True\n",
    "from-import mark": "from pytest import mark\n\n@mark.skip\ndef test_a():\n    assert True\n",
    "aliased mark": "from pytest import mark as m\n\n@m.timing\ndef test_a():\n    assert True\n",
    "aliased skip": "from pytest import skip as s\n\ndef test_a():\n    s('x')\n",
    "unittest skip": "import unittest\n\n@unittest.skip('x')\ndef test_a():\n    assert True\n",
    "unittest skipIf": "import unittest\n\n@unittest.skipIf(True, 'x')\ndef test_a():\n    assert True\n",
    "unittest expectedFailure": "import unittest\n\n@unittest.expectedFailure\ndef test_a():\n    assert True\n",
    "pytestmark attribute assignment": "class TestK:\n    pass\n\nTestK.pytestmark = []\n",
    "bare imported decorator": "from unittest import expectedFailure\n\n@expectedFailure\ndef test_a():\n    assert True\n",
    "bare reference to an imported skip": "from pytest import skip\n\nstop = skip\n\ndef test_a():\n    stop('x')\n",
    "raise of an imported SkipTest": "from unittest import SkipTest\n\ndef test_a():\n    raise SkipTest\n",
    "star import then a bare reference": "from pytest import *\n\ndef test_a():\n    f = xfail\n    f('x')\n",
    "collect_ignore append": "collect_ignore.append('x.py')\n\ndef test_a():\n    assert True\n",
    "collect_ignore_glob extend": "collect_ignore_glob.extend(['*.py'])\n\ndef test_a():\n    assert True\n",
    "pytestmark insert": "pytestmark.insert(0, 1)\n\ndef test_a():\n    assert True\n",
    "deleting an exclusion": "del collect_ignore[0]\n\ndef test_a():\n    assert True\n",
    "an import of a skip function": "from pytest import skip\n\ndef test_a():\n    assert True\n",
    "skipTest call": "def test_a(self):\n    self.skipTest('x')\n",
    "SkipTest raise": "import unittest\n\ndef test_a():\n    raise unittest.SkipTest('x')\n",
    "param with a skip mark": "@pytest.mark.parametrize('v', [pytest.param(1, marks=pytest.mark.skip)])\ndef test_a(v):\n    assert v\n",
    "param with xfail marks": "@pytest.mark.parametrize('v', [pytest.param(1, marks=[pytest.mark.xfail])])\ndef test_a(v):\n    assert v\n",
}


def _module(construct: str | None) -> str:
    return CLEAN_TEST if construct is None else CLEAN_TEST + "\n\n" + construct


@pytest.mark.parametrize("name", sorted(CONSTRUCTS))
def test_a_construct_added_to_an_existing_test_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, {"tests/test_a.py": _module(CONSTRUCTS[name])})
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "tests/test_a.py" in output
    assert "added" in output


@pytest.mark.parametrize("name", sorted(CONSTRUCTS))
def test_a_construct_removed_from_an_existing_test_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST},
        base=base_files() | {"tests/test_a.py": _module(CONSTRUCTS[name])},
    )
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "removed" in output


@pytest.mark.parametrize(
    "name", ["skip decorator", "pytestmark", "collect_ignore", "unittest skip"]
)
def test_a_construct_in_a_new_test_file_counts_as_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, {"tests/test_new.py": _module(CONSTRUCTS[name])})
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "tests/test_new.py" in output and "added" in output


@pytest.mark.parametrize(
    "name", ["skip decorator", "pytestmark", "collect_ignore", "unittest skip"]
)
def test_a_construct_in_a_deleted_test_file_counts_as_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    base = base_files() | {"tests/test_old.py": _module(CONSTRUCTS[name])}
    repo = scenario(tmp_path, {"tests/test_old.py": None}, base=base)
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "tests/test_old.py" in output and "removed" in output


def test_a_construct_in_a_nested_test_file_and_a_conftest_is_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(
        tmp_path, {"tests/sub/deep/conftest.py": _module(CONSTRUCTS["modifyitems hook"])}
    )
    code, output = check(repo, capsys)
    assert code == 1 and "tests/sub/deep/conftest.py" in output


def test_the_finding_names_the_file_the_scope_and_the_construct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = (
        CLEAN_TEST
        + "\n\nclass TestK:\n    @pytest.mark.skip(reason='why')\n    def test_m(self):\n        pass\n"
    )
    repo = scenario(tmp_path, {"tests/test_a.py": module})
    _, output = check(repo, capsys)
    assert "tests/test_a.py" in output
    assert "TestK" in output and "test_m" in output
    assert "pytest.mark.skip(reason='why')" in output


# --- words held as data, and changes that are not a weakening ---

STRING_ONLY = [
    'ARGV = ["--deselect", "tests/test_x.py::test_y", "-m", "not timing", "-k", "not slow"]\n',
    'TOY = """\nimport pytest\n\n@pytest.mark.skip\ndef test_x():\n    pass\n\npytestmark = []\n"""\n',
    'WORDS = "skip skipif xfail importorskip pytest_deselected collect_ignore pytestmark"\n',
    'def test_b():\n    toy = """\n@pytest.mark.xfail\ndef test_t():\n    pass\n"""\n    assert toy\n',
    "# pytest.skip('only a comment'); @pytest.mark.timing\n",
    'def test_b():\n    """A docstring that says pytest.mark.skip and pytest_deselected."""\n',
    'def test_b():\n    status = "skip"\n    skipped = 3\n    assert status and skipped\n',
    'def test_b():\n    kwargs = {"skipped": [1], "xfail": True}\n    assert kwargs\n',
]


@pytest.mark.parametrize("extra", STRING_ONLY, ids=[str(i) for i in range(len(STRING_ONLY))])
def test_the_same_words_held_only_as_data_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: str
) -> None:
    repo = scenario(tmp_path, {"tests/test_a.py": CLEAN_TEST + "\n\n" + extra})
    code, output = check(repo, capsys)
    assert code == 0, output


def test_a_parametrize_mark_added_or_removed_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parametrized = (
        CLEAN_TEST + "\n\n@pytest.mark.parametrize('v', [1, 2])\ndef test_b(v):\n    assert v\n"
    )
    repo = scenario(tmp_path, {"tests/test_a.py": parametrized})
    assert check(repo, capsys)[0] == 0
    removed = scenario(
        tmp_path / "second",
        {"tests/test_a.py": CLEAN_TEST},
        base=base_files() | {"tests/test_a.py": parametrized},
    )
    assert check(removed, capsys)[0] == 0


def test_reformatting_a_construct_without_changing_it_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = CLEAN_TEST + "\n\n@pytest.mark.skip(reason='r')\ndef test_b():\n    pass\n"
    after = (
        CLEAN_TEST + '\n\n@pytest.mark.skip(\n    reason = "r"\n)\n\n\ndef test_b():\n    pass\n'
    )
    repo = scenario(
        tmp_path, {"tests/test_a.py": after}, base=base_files() | {"tests/test_a.py": before}
    )
    code, output = check(repo, capsys)
    assert code == 0, output


def test_a_plain_new_test_a_deleted_plain_test_and_a_non_python_file_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = base_files() | {"tests/test_old.py": CLEAN_TEST, "tests/data.txt": "skip\n"}
    repo = scenario(
        tmp_path,
        {"tests/test_new.py": CLEAN_TEST, "tests/test_old.py": None, "tests/data.txt": "xfail\n"},
        base=base,
    )
    assert check(repo, capsys)[0] == 0


def test_a_change_outside_tests_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = scenario(
        tmp_path, {"scripts/tool.py": "import pytest\n\npytest.skip('x')\n", "README.md": "x\n"}
    )
    assert check(repo, capsys)[0] == 0


def test_an_unchanged_branch_passes_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(tmp_path, {"README.md": "new\n"})
    code, output = check(repo, capsys)
    assert code == 0
    assert "no weakening" in output.lower()


# --- the baseline is the merge-base, for the file list and for every comparison ---


def test_a_skip_added_on_the_branch_is_found_even_if_the_base_tip_later_gained_the_same_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skipped = CLEAN_TEST + "\n\n" + CONSTRUCTS["skip decorator"].replace("test_a", "test_b")
    repo = Repo(tmp_path / "repo")
    repo.commit(base_files(), "base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit({"tests/test_a.py": skipped}, "branch adds a skip")
    repo.git("checkout", "-q", "main")
    repo.commit({"tests/test_a.py": skipped}, "main gains the same skip later")
    repo.git("checkout", "-q", "feature")
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "added" in output


def test_a_skip_removed_on_the_base_after_the_branch_point_is_not_blamed_on_the_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skipped = CLEAN_TEST + "\n\n" + CONSTRUCTS["skip decorator"].replace("test_a", "test_b")
    repo = Repo(tmp_path / "repo")
    repo.commit(base_files() | {"tests/test_a.py": skipped}, "base has a skip")
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit({"tests/test_other.py": CLEAN_TEST}, "branch adds an unrelated test")
    repo.git("checkout", "-q", "main")
    repo.commit({"tests/test_a.py": CLEAN_TEST}, "main drops the skip")
    repo.git("checkout", "-q", "feature")
    code, output = check(repo, capsys)
    assert code == 0, output


def test_a_file_changed_only_on_the_base_after_the_branch_point_is_not_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = Repo(tmp_path / "repo")
    repo.commit(base_files(), "base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit({"README.md": "branch\n"}, "branch")
    repo.git("checkout", "-q", "main")
    repo.commit({"tests/test_a.py": _module(CONSTRUCTS["skip decorator"])}, "main adds a skip")
    repo.commit({"proof/contract.yaml": _edited(lambda c: c["gates"].pop())}, "main drops a gate")
    repo.commit({"pyproject.toml": PYPROJECT.replace("-v ", "")}, "main changes pytest settings")
    repo.git("checkout", "-q", "feature")
    code, output = check(repo, capsys)
    assert code == 0, output


def test_the_contract_and_settings_are_compared_with_the_merge_base_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = Repo(tmp_path / "repo")
    repo.commit(base_files(), "base")
    repo.git("checkout", "-q", "-b", "feature")
    weaker = _edited(lambda c: _gate(c, "unit_full").update(command=["pytest", "-x"]))
    repo.commit(
        {"proof/contract.yaml": weaker, "pyproject.toml": PYPROJECT.replace("-v ", "")}, "weaken"
    )
    repo.git("checkout", "-q", "main")
    repo.commit(
        {"proof/contract.yaml": weaker, "pyproject.toml": PYPROJECT.replace("-v ", "")}, "main too"
    )
    repo.git("checkout", "-q", "feature")
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "unit_full" in output and "addopts" in output


def test_an_unrelated_base_history_is_a_failure_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = Repo(tmp_path / "repo")
    repo.commit(base_files(), "base")
    repo.git("checkout", "-q", "--orphan", "island")
    repo.commit({"x.txt": "x\n"}, "unrelated")
    repo.git("checkout", "-q", "main")
    code, output = check(repo, capsys, base="island")
    assert code == 2, output


# --- the contract: gates, phases, flags, membership and conditions ---


def _contract_case(edit: Callable[[dict[str, Any]], None]) -> dict[str, str | bytes | None]:
    return {"proof/contract.yaml": _edited(edit)}


CONTRACT_WEAKENINGS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "command changed": (
        lambda c: _gate(c, "unit_full").update(command=["pytest", "tests/", "-x"]),
        "gate unit_full command changed",
    ),
    "command argument dropped": (
        lambda c: _gate(c, "unit_full").update(command=["pytest", "tests/"]),
        "gate unit_full command changed",
    ),
    "required set to false": (
        lambda c: _gate(c, "unit_full").update(required=False),
        "gate unit_full required flag set to false",
    ),
    "absent required set to false": (
        lambda c: _gate(c, "format").update(required=False),
        "gate format required flag set to false",
    ),
    "gate removed": (lambda c: c["gates"].remove(_gate(c, "unit_full")), "gate unit_full removed"),
    "phase removed from a mode": (
        lambda c: c["modes"]["full"]["phases"].remove("unit_full"),
        "phase unit_full removed from mode full",
    ),
    "mode removed": (lambda c: c["modes"].pop("targeted"), "mode targeted removed"),
    "phases emptied": (
        lambda c: c["modes"]["full"].update(phases=[]),
        "gate unit_full no longer runs in mode full",
    ),
    "gate moved out of full mode by its phase": (
        lambda c: _gate(c, "unit_full").update(phase="retired"),
        "gate unit_full no longer runs in mode full",
    ),
    "gate that needs llm_boundary moved out of full mode by its phase": (
        lambda c: _gate(c, "promptfoo").update(phase="retired"),
        "gate promptfoo no longer runs in mode full with --llm-boundary",
    ),
    "required gate given a when condition": (
        lambda c: _gate(c, "unit_full").update(when="llm_boundary"),
        "gate unit_full execution condition added or changed",
    ),
    "when condition changed": (
        lambda c: _gate(c, "promptfoo").update(when="never"),
        "gate promptfoo execution condition added or changed",
    ),
    "targets placeholder changed": (
        lambda c: _gate(c, "unit_targeted").update(targets_placeholder="{x}"),
        "gate unit_targeted targets_placeholder changed",
    ),
    "env_clear added to an existing gate": (
        lambda c: _gate(c, "unit_full").update(env_clear=["PATH"]),
        "gate unit_full gained env_clear names",
    ),
    "gate renamed": (
        lambda c: _gate(c, "unit_full").update(id="unit_everything"),
        "gate unit_full removed",
    ),
}


@pytest.mark.parametrize("name", sorted(CONTRACT_WEAKENINGS))
def test_a_weakened_contract_fails_with_the_finding_of_its_own_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    edit, needle = CONTRACT_WEAKENINGS[name]
    repo = scenario(tmp_path, _contract_case(edit))
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "proof/contract.yaml" in output
    assert needle in output, output


def test_a_gate_moved_out_of_full_mode_names_the_gate_and_the_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(
        tmp_path, _contract_case(CONTRACT_WEAKENINGS["gate moved out of full mode by its phase"][0])
    )
    _, output = check(repo, capsys)
    assert "unit_full" in output and "full" in output and "no longer runs" in output


def test_a_new_when_condition_on_a_required_gate_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(
        tmp_path, _contract_case(CONTRACT_WEAKENINGS["required gate given a when condition"][0])
    )
    _, output = check(repo, capsys)
    assert "unit_full" in output and "when" in output


CONTRACT_STRENGTHENINGS: dict[str, Callable[[dict[str, Any]], None]] = {
    "gate added": lambda c: c["gates"].append(
        {"id": "extra", "phase": "format", "classify": "lint", "command": ["true"]}
    ),
    "phase added": lambda c: c["modes"]["full"]["phases"].append("extra_phase"),
    "mode added": lambda c: c["modes"].update(extra={"phases": ["format"]}),
    "key added to a gate": lambda c: _gate(c, "unit_full").update(shard="split"),
    "shards declared": lambda c: c["modes"]["full"].update(shards=3),
    "env_clear on a new gate": lambda c: c["gates"].append(
        {"id": "t", "phase": "format", "classify": "test", "command": ["x"], "env_clear": ["A"]}
    ),
    "required false to true": lambda c: _gate(c, "security_bandit").update(required=True),
    "when condition removed": lambda c: _gate(c, "promptfoo").pop("when"),
    "required true made explicit": lambda c: _gate(c, "format").update(required=True),
    "gates reordered": lambda c: c["gates"].reverse(),
}


@pytest.mark.parametrize("name", sorted(CONTRACT_STRENGTHENINGS))
def test_a_contract_that_only_adds_or_tightens_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, _contract_case(CONTRACT_STRENGTHENINGS[name]))
    code, output = check(repo, capsys)
    assert code == 0, output


def test_the_same_contract_written_differently_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = (
        "# a comment\nschema_version: 1\nname: toy\nrunner: native\nmodes:\n"
        "  targeted: {phases: [format, unit_targeted]}\n"
        "  full: {phases: [format, unit_targeted, unit_full, security, adversarial]}\ngates:\n"
        + "".join(
            "  - " + yaml.safe_dump(g, default_flow_style=True).strip().replace("\n", " ") + "\n"
            for g in BASE_CONTRACT["gates"]
        )
    )
    repo = scenario(tmp_path, {"proof/contract.yaml": flow})
    code, output = check(repo, capsys)
    assert code == 0, output


def test_a_deleted_contract_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = scenario(tmp_path, {"proof/contract.yaml": None})
    code, output = check(repo, capsys)
    assert code == 1 and "proof/contract.yaml" in output


def test_a_contract_that_is_not_valid_yaml_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(tmp_path, {"proof/contract.yaml": "gates: [unclosed\n"})
    assert check(repo, capsys)[0] == 2


def test_a_contract_that_is_not_a_mapping_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(tmp_path, {"proof/contract.yaml": "- a\n- b\n"})
    assert check(repo, capsys)[0] == 2


def test_the_effective_membership_rule_agrees_with_the_proof_runner(tmp_path: Path) -> None:
    """The checker's own reading of 'which gates run in a mode' is the loader's."""
    from scripts.proof.check_not_weakened import effective_gates
    from scripts.proof.contract import load_contract

    path = tmp_path / "contract.yaml"
    path.write_text(_contract_text(BASE_CONTRACT), encoding="utf-8")
    loaded = load_contract(path)
    for mode in ("targeted", "full"):
        for boundary in (False, True):
            assert effective_gates(BASE_CONTRACT, mode, boundary) == {
                g.id for g in loaded.gates_for_mode(mode, llm_boundary=boundary)
            }
    real = load_contract(Path(__file__).resolve().parents[1] / "proof" / "contract.yaml")
    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "proof" / "contract.yaml").read_text()
    )
    for mode in real.modes:
        for boundary in (False, True):
            assert effective_gates(raw, mode, boundary) == {
                g.id for g in real.gates_for_mode(mode, llm_boundary=boundary)
            }


# --- pytest settings and the durations file ---


@pytest.mark.parametrize(
    "new_text",
    [
        PYPROJECT.replace("-v --tb=short", "-v --tb=short -m 'not slow'"),
        PYPROJECT.replace("-v --tb=short", "-v --tb=short --deselect tests/x.py::y"),
        PYPROJECT.replace('markers = ["timing: serial"]', 'markers = ["timing: serial", "new: n"]'),
        PYPROJECT.replace('markers = ["timing: serial"]\n', ""),
        PYPROJECT.replace(
            "[tool.pytest.ini_options]\n", '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'
        ),
    ],
    ids=["marker filter", "deselect", "marker added", "setting removed", "setting added"],
)
def test_a_changed_pytest_setting_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], new_text: str
) -> None:
    repo = scenario(tmp_path, {"pyproject.toml": new_text})
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "pyproject.toml" in output and "pytest" in output


def test_a_change_outside_the_pytest_table_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(
        tmp_path, {"pyproject.toml": PYPROJECT.replace("line-length = 100", "line-length = 88")}
    )
    assert check(repo, capsys)[0] == 0


def test_a_pytest_table_reordered_or_reformatted_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        '[project]\nname = "toy"\n\n[tool.pytest.ini_options]\n'
        'markers = [\n  "timing: serial",\n]\naddopts = "-v --tb=short"\n\n[tool.ruff]\nline-length = 100\n'
    )
    repo = scenario(tmp_path, {"pyproject.toml": text})
    assert check(repo, capsys)[0] == 0


def test_a_pyproject_that_cannot_be_parsed_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(tmp_path, {"pyproject.toml": "[tool.pytest\n"})
    assert check(repo, capsys)[0] == 2


def test_a_new_pytest_ini_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = scenario(tmp_path, {"pytest.ini": "[pytest]\naddopts = -m 'not timing'\n"})
    code, output = check(repo, capsys)
    assert code == 1 and "pytest.ini" in output


def test_a_changed_durations_file_fails_and_an_unchanged_one_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(tmp_path / "a", {".test_durations": '{"tests/x.py::y": 1.0}\n'})
    code, output = check(repo, capsys)
    assert code == 1 and ".test_durations" in output
    clean = scenario(tmp_path / "b", {"README.md": "x\n"})
    assert check(clean, capsys)[0] == 0


# --- failures to check are never a pass ---


def test_a_bad_base_ref_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(tmp_path, {"README.md": "x\n"})
    code, output = check(repo, capsys, base="no-such-ref")
    assert code == 2
    assert "no-such-ref" in output


def test_a_directory_that_is_not_a_repository_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "plain").mkdir()
    code = main(["--base", "main", "--repo", str(tmp_path / "plain")])
    assert code == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    "broken",
    ["def test_a(:\n", "def test_a():\n  x = (\n", "\x00not python"],
    ids=["syntax", "unclosed", "nul-byte"],
)
def test_an_unparsable_changed_test_file_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], broken: str
) -> None:
    repo = scenario(tmp_path, {"tests/test_a.py": broken})
    assert check(repo, capsys)[0] == 2


def test_an_unparsable_test_file_at_the_base_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST},
        base=base_files() | {"tests/test_a.py": "def broken(:\n"},
    )
    assert check(repo, capsys)[0] == 2


def test_a_test_file_that_is_not_utf8_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(tmp_path, {"tests/test_a.py": b"\xff\xfe\x00 def"})
    assert check(repo, capsys)[0] == 2


def test_findings_and_a_failure_to_check_together_are_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": _module(CONSTRUCTS["skip decorator"]), "tests/test_b.py": "def x(:\n"},
    )
    assert check(repo, capsys)[0] == 2


def test_every_finding_is_printed_not_only_the_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scenario(
        tmp_path,
        {
            "tests/test_a.py": _module(CONSTRUCTS["skip decorator"]),
            "tests/test_b.py": _module(CONSTRUCTS["pytestmark"]),
            ".test_durations": "{}\n\n",
            "pyproject.toml": PYPROJECT.replace("-v ", ""),
        },
    )
    code, output = check(repo, capsys)
    assert code == 1
    for needle in ("tests/test_a.py", "tests/test_b.py", ".test_durations", "pyproject.toml"):
        assert needle in output


# --- a git hook's environment must not redirect anything ---


def _point_git_at(monkeypatch: pytest.MonkeyPatch, decoy: Repo) -> None:
    monkeypatch.setenv("GIT_DIR", str(decoy.path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy.path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy.path / ".git" / "index"))


def test_the_test_repository_helper_is_not_redirected_by_a_hooks_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = Repo(tmp_path / "decoy")
    decoy_head = decoy.commit({"decoy.txt": "keep\n"}, "decoy")
    _point_git_at(monkeypatch, decoy)
    repo = scenario(tmp_path / "toy", {"tests/test_a.py": _module(CONSTRUCTS["skip decorator"])})
    assert (repo.path / ".git").is_dir()
    assert decoy.git("rev-parse", "HEAD") == decoy_head
    assert decoy.git("status", "--porcelain") == ""
    assert decoy.git("log", "--format=%s") == "decoy"


def test_the_checker_reads_the_repository_it_is_given_not_the_one_a_hook_runs_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = Repo(tmp_path / "decoy")
    decoy_head = decoy.commit({"decoy.txt": "keep\n"}, "decoy")
    repo = scenario(tmp_path / "toy", {"tests/test_a.py": _module(CONSTRUCTS["skip decorator"])})
    _point_git_at(monkeypatch, decoy)
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "tests/test_a.py" in output
    assert decoy.git("rev-parse", "HEAD") == decoy_head
    assert decoy.git("status", "--porcelain") == ""


def test_the_checkers_list_of_git_variables_covers_what_git_reports() -> None:
    done = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        capture_output=True,
        text=True,
        check=True,
        env=clean_git_env(),
    )
    assert set(done.stdout.split()) <= GIT_LOCAL_ENV


# --- editing a construct that is already there must be seen too ---

HOOK_EDITS = {
    "modifyitems drops the items": (
        "def pytest_collection_modifyitems(items):\n    pass\n",
        "def pytest_collection_modifyitems(items):\n    items.clear()\n",
    ),
    "ignore_collect starts ignoring": (
        "def pytest_ignore_collect(collection_path):\n    return False\n",
        "def pytest_ignore_collect(collection_path):\n    return True\n",
    ),
    "deselected hook gains a body": (
        "def pytest_deselected(items):\n    pass\n",
        "def pytest_deselected(items):\n    items[:] = []\n",
    ),
    "a hook is edited without touching its name": (
        "def pytest_collection_modifyitems(config, items):\n    for item in items:\n        item.add_marker('x')\n",
        "def pytest_collection_modifyitems(config, items):\n    for item in items:\n        item.add_marker('y')\n",
    ),
}


@pytest.mark.parametrize("name", sorted(HOOK_EDITS))
def test_changing_the_body_of_an_existing_collection_hook_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    before, after = HOOK_EDITS[name]
    repo = scenario(
        tmp_path, {"tests/conftest.py": after}, base=base_files() | {"tests/conftest.py": before}
    )
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "tests/conftest.py" in output and "hook definition" in output


def test_an_untouched_collection_hook_does_not_fail_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = HOOK_EDITS["modifyitems drops the items"][0]
    repo = scenario(
        tmp_path,
        {"tests/conftest.py": hook, "README.md": "new\n"},
        base=base_files() | {"tests/conftest.py": hook},
    )
    assert check(repo, capsys)[0] == 0


ALIAS_EDITS = {
    "skip becomes xfail": (
        "from pytest import skip as s\n\ndef test_a():\n    s('r')\n",
        "from pytest import xfail as s\n\ndef test_a():\n    s('r')\n",
    ),
    "skip becomes importorskip": (
        "from pytest import skip as s\n\ndef test_a():\n    s('r')\n",
        "from pytest import importorskip as s\n\ndef test_a():\n    s('r')\n",
    ),
    "a bare reference is re-pointed": (
        "from pytest import skip\n\nstop = skip\n",
        "from pytest import xfail as skip\n\nstop = skip\n",
    ),
}


@pytest.mark.parametrize("name", sorted(ALIAS_EDITS))
def test_changing_what_an_aliased_skip_import_points_at_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    before, after = ALIAS_EDITS[name]
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST + "\n\n" + after},
        base=base_files() | {"tests/test_a.py": CLEAN_TEST + "\n\n" + before},
    )
    code, output = check(repo, capsys)
    assert code == 1, output


def test_an_unchanged_aliased_import_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = CLEAN_TEST + "\n\n" + ALIAS_EDITS["skip becomes xfail"][0]
    repo = scenario(tmp_path, {"README.md": "x\n"}, base=base_files() | {"tests/test_a.py": text})
    assert check(repo, capsys)[0] == 0


# --- a malformed contract is a failure to check, and a crash is never exit 1 ---

MALFORMED = {
    "env_clear is a number": lambda c: _gate(c, "unit_full").update(env_clear=1),
    "env_clear holds a list": lambda c: _gate(c, "unit_full").update(env_clear=[["PATH"]]),
    "env_clear is a string": lambda c: _gate(c, "unit_full").update(env_clear="PATH"),
    "gates is a number": lambda c: c.update(gates=5),
    "a gate is a number": lambda c: c["gates"].append(7),
    "phases is a number": lambda c: c["modes"]["full"].update(phases=7),
    "a phase is a list": lambda c: c["modes"]["full"]["phases"].append(["x"]),
    "modes is a list": lambda c: c.update(modes=[1]),
    "a mode is a number": lambda c: c["modes"].update(full=3),
    "a gate has no id": lambda c: c["gates"].append({"phase": "format"}),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_malformed_nested_contract_value_is_a_failure_to_check_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, _contract_case(MALFORMED[name]))
    code, output = check(repo, capsys)
    assert code == 2, output
    assert "proof/contract.yaml" in output
    assert "Traceback" not in output


def test_an_unexpected_exception_is_a_failure_to_check_never_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.proof.check_not_weakened as module

    def explode(*args: object, **kwargs: object) -> None:
        raise ZeroDivisionError("kaboom")

    monkeypatch.setattr(module, "contract_findings", explode)
    repo = scenario(tmp_path, _contract_case(CONTRACT_STRENGTHENINGS["gate added"]))
    code, output = check(repo, capsys)
    assert code == 2, output
    assert "kaboom" in output


# --- exclusions changed in place, and aliases reused in different scopes ---


@pytest.mark.parametrize(
    "edit",
    [
        "collect_ignore.append('test_a.py')",
        "collect_ignore.extend(['test_a.py'])",
        "collect_ignore_glob.insert(0, '*.py')",
        "collect_ignore[:] = ['test_a.py']",
    ],
)
def test_changing_an_existing_exclusion_list_in_place_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edit: str
) -> None:
    before = "collect_ignore = []\ncollect_ignore_glob = []\n"
    repo = scenario(
        tmp_path,
        {"tests/conftest.py": before + edit + "\n"},
        base=base_files() | {"tests/conftest.py": before},
    )
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "tests/conftest.py" in output


TWO_SCOPES = (
    "def test_a():\n    from pytest import {a} as s\n    s('r')\n\n"
    "def test_b():\n    from pytest import {b} as s\n    s('r')\n"
)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (("skip", "xfail"), ("xfail", "xfail")),
        (("skip", "xfail"), ("skip", "skip")),
        (("skip", "skip"), ("skip", "importorskip")),
    ],
    ids=["first function re-pointed", "second function re-pointed", "last function re-pointed"],
)
def test_the_same_alias_in_two_functions_is_told_apart_by_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    before: tuple[str, str],
    after: tuple[str, str],
) -> None:
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST + "\n\n" + TWO_SCOPES.format(a=after[0], b=after[1])},
        base=base_files()
        | {"tests/test_a.py": CLEAN_TEST + "\n\n" + TWO_SCOPES.format(a=before[0], b=before[1])},
    )
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "test_a" in output or "test_b" in output


def test_an_unchanged_aliased_import_file_passes_and_an_added_import_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = CLEAN_TEST + "\n\n" + TWO_SCOPES.format(a="skip", b="xfail")
    untouched = scenario(
        tmp_path / "a", {"README.md": "x\n"}, base=base_files() | {"tests/test_a.py": text}
    )
    assert check(untouched, capsys)[0] == 0
    # The file skips, so it is compared whole: even an unrelated import is a reason to look.
    edited = scenario(
        tmp_path / "b",
        {"tests/test_a.py": text + "\nimport os\nfrom pytest import fixture, approx\n"},
        base=base_files() | {"tests/test_a.py": text},
    )
    code, output = check(edited, capsys)
    assert code == 1, output
    assert "file with skip-like constructs changed" in output


# --- a hook's docstring is not behaviour; the loader's types are the contract's ---


def test_changing_only_the_docstring_of_a_collection_hook_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = 'def pytest_collection_modifyitems(items):\n    """Old words."""\n    items.sort()\n'
    after = 'def pytest_collection_modifyitems(items):\n    """New, longer words."""\n    items.sort()\n'
    repo = scenario(
        tmp_path, {"tests/conftest.py": after}, base=base_files() | {"tests/conftest.py": before}
    )
    code, output = check(repo, capsys)
    assert code == 0, output


def test_changing_a_hooks_body_beside_an_unchanged_docstring_still_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = '    """Same words."""\n'
    before = "def pytest_collection_modifyitems(items):\n" + doc + "    items.sort()\n"
    after = "def pytest_collection_modifyitems(items):\n" + doc + "    items.clear()\n"
    repo = scenario(
        tmp_path, {"tests/conftest.py": after}, base=base_files() | {"tests/conftest.py": before}
    )
    assert check(repo, capsys)[0] == 1


def test_a_hook_that_is_only_a_docstring_can_gain_a_body_only_with_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = 'def pytest_ignore_collect(collection_path):\n    """Nothing yet."""\n'
    after = 'def pytest_ignore_collect(collection_path):\n    """Nothing yet."""\n    return True\n'
    repo = scenario(
        tmp_path, {"tests/conftest.py": after}, base=base_files() | {"tests/conftest.py": before}
    )
    assert check(repo, capsys)[0] == 1


MALFORMED_LIKE_THE_LOADER = {
    "required is 0": lambda c: _gate(c, "unit_full").update(required=0),
    "required is the string false": lambda c: _gate(c, "unit_full").update(required="false"),
    "required is null": lambda c: _gate(c, "unit_full").update(required=None),
    "command is a string": lambda c: _gate(c, "unit_full").update(command="pytest tests/"),
    "command is empty": lambda c: _gate(c, "unit_full").update(command=[]),
    "command holds a number": lambda c: _gate(c, "unit_full").update(command=["pytest", 1]),
    "phase is a number": lambda c: _gate(c, "unit_full").update(phase=3),
    "when is a number": lambda c: _gate(c, "promptfoo").update(when=5),
    "targets_placeholder is a list": lambda c: _gate(c, "unit_targeted").update(
        targets_placeholder=["{targets}"]
    ),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_LIKE_THE_LOADER))
def test_a_value_the_real_loader_rejects_is_a_failure_to_check_never_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, _contract_case(MALFORMED_LIKE_THE_LOADER[name]))
    code, output = check(repo, capsys)
    assert code == 2, output
    assert "proof/contract.yaml" in output


def test_the_checked_in_contract_satisfies_the_checkers_shape_rules() -> None:
    from scripts.proof.check_not_weakened import _validate_shape

    root = Path(__file__).resolve().parents[1]
    loaded = yaml.safe_load((root / "proof" / "contract.yaml").read_text(encoding="utf-8"))
    _validate_shape(loaded, "proof/contract.yaml")


# --- whatever the real loader rejects is a failure to check, however it is malformed ---

LOADER_REJECTS = {
    "a gate id repeated": lambda c: c["gates"].append(dict(_gate(c, "format"))),
    "a command part that is blank": lambda c: _gate(c, "unit_full").update(command=["pytest", " "]),
    "a classify the loader does not know": lambda c: _gate(c, "unit_full").update(classify="magic"),
    "a shard word the loader does not know": lambda c: _gate(c, "unit_full").update(shard="all"),
    "shards that is zero": lambda c: c["modes"]["full"].update(shards=0),
    "shards that is a boolean": lambda c: c["modes"]["full"].update(shards=True),
    "shards on a mode that is not full": lambda c: c["modes"]["targeted"].update(shards=2),
    "a schema version other than 1": lambda c: c.update(schema_version=2),
    "a runner the loader does not allow": lambda c: c.update(runner="dagger"),
    "no name": lambda c: c.pop("name"),
    "an empty gate list": lambda c: c.update(gates=[]),
    "a leader that no split gate covers": lambda c: _gate(c, "unit_full").update(shard="leader"),
    "an independent gate made a leader": lambda c: (
        _gate(c, "unit_full").update(shard="split"),
        _gate(c, "security_bandit").update(shard="leader"),
    ),
    "a split gate that is not a test gate": lambda c: _gate(c, "security_bandit").update(
        shard="split"
    ),
}


@pytest.mark.parametrize("name", sorted(LOADER_REJECTS))
def test_a_contract_the_real_loader_rejects_is_a_failure_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, _contract_case(LOADER_REJECTS[name]))
    code, output = check(repo, capsys)
    assert code == 2, output
    assert "proof/contract.yaml" in output


def test_the_base_contract_is_judged_by_the_loader_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = _edited(lambda c: c["gates"].append(dict(_gate(c, "format"))))
    repo = scenario(
        tmp_path, {"README.md": "x\n"}, base=base_files() | {"proof/contract.yaml": broken}
    )
    assert check(repo, capsys)[0] == 2


def test_the_toy_base_contract_is_one_the_real_loader_accepts(tmp_path: Path) -> None:
    """The fixtures above only mean something if their starting point is valid."""
    from scripts.proof.contract import load_contract

    path = tmp_path / "contract.yaml"
    path.write_text(_contract_text(BASE_CONTRACT), encoding="utf-8")
    assert len(load_contract(path).gates) == len(BASE_CONTRACT["gates"])


# --- the condition a skip runs under, and markers added at run time ---

CONDITION_EDITS = {
    "if False becomes if True": (
        "def test_b():\n    if False:\n        pytest.skip('r')\n",
        "def test_b():\n    if True:\n        pytest.skip('r')\n",
    ),
    "a guard is dropped": (
        "def test_b(flag):\n    if flag:\n        pytest.skip('r')\n",
        "def test_b(flag):\n    pytest.skip('r')\n",
    ),
    "the else branch gains the skip": (
        "def test_b(flag):\n    if flag:\n        pass\n    else:\n        pytest.skip('r')\n",
        "def test_b(flag):\n    if not flag:\n        pass\n    else:\n        pytest.skip('r')\n",
    ),
    "a constant that guards a skip flips": (
        "COND = False\n\ndef test_b():\n    if COND:\n        pytest.skip('r')\n",
        "COND = True\n\ndef test_b():\n    if COND:\n        pytest.skip('r')\n",
    ),
    "a constant read by skipif flips": (
        "SKIP = False\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
        "SKIP = True\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "a constant read through another constant flips": (
        "FLAG = False\nSKIP = FLAG\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
        "FLAG = True\nSKIP = FLAG\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "an and guard flips": (
        "def test_b():\n    False and pytest.skip('r')\n",
        "def test_b():\n    True and pytest.skip('r')\n",
    ),
    "an or guard flips": (
        "def test_b():\n    True or pytest.skip('r')\n",
        "def test_b():\n    False or pytest.skip('r')\n",
    ),
    "a later operand of a longer and guard flips": (
        "def test_b(a):\n    a and True and pytest.skip('r')\n",
        "def test_b(a):\n    a and False and pytest.skip('r')\n",
    ),
    "a local flag flips": (
        "def test_b():\n    skip_test = False\n    if skip_test:\n        pytest.skip('r')\n",
        "def test_b():\n    skip_test = True\n    if skip_test:\n        pytest.skip('r')\n",
    ),
    "a parameter default flips": (
        "def test_b(flag=False):\n    if flag:\n        pytest.skip('r')\n",
        "def test_b(flag=True):\n    if flag:\n        pytest.skip('r')\n",
    ),
    "a local that shadows a module constant flips": (
        "SKIP = False\n\ndef test_b():\n    SKIP = False\n    if SKIP:\n        pytest.skip('r')\n",
        "SKIP = False\n\ndef test_b():\n    SKIP = True\n    if SKIP:\n        pytest.skip('r')\n",
    ),
    "a method local that shadows a class attribute flips": (
        "class TestK:\n    SKIP = False\n\n    def test_b(self):\n        SKIP = False\n        if SKIP:\n            pytest.skip('r')\n",
        "class TestK:\n    SKIP = False\n\n    def test_b(self):\n        SKIP = True\n        if SKIP:\n            pytest.skip('r')\n",
    ),
    "a value assigned before the guard changes while the same name is reassigned after it": (
        "def test_b():\n    flag = False\n    if flag:\n        pytest.skip('r')\n    flag = False\n",
        "def test_b():\n    flag = True\n    if flag:\n        pytest.skip('r')\n    flag = False\n",
    ),
    "a decorator constant flips while the body shadows it": (
        "SKIP = False\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    SKIP = False\n    assert not SKIP\n",
        "SKIP = True\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    SKIP = False\n    assert not SKIP\n",
    ),
    "a loop iterable that guards a skip gains a member": (
        "def test_b():\n    for case in []:\n        pytest.skip('r')\n",
        "def test_b():\n    for case in [1]:\n        pytest.skip('r')\n",
    ),
    "a module list a loop iterates gains a member": (
        "CASES = []\n\ndef test_b():\n    for case in CASES:\n        pytest.skip('r')\n",
        "CASES = [1]\n\ndef test_b():\n    for case in CASES:\n        pytest.skip('r')\n",
    ),
    "an augmented write to a guard flips": (
        "def test_b():\n    flag = False\n    flag |= False\n    if flag:\n        pytest.skip('r')\n",
        "def test_b():\n    flag = False\n    flag |= True\n    if flag:\n        pytest.skip('r')\n",
    ),
    "an instance attribute set before the guard flips": (
        "class TestK:\n    def test_b(self):\n        self.disabled = False\n        if self.disabled:\n            pytest.skip('r')\n",
        "class TestK:\n    def test_b(self):\n        self.disabled = True\n        if self.disabled:\n            pytest.skip('r')\n",
    ),
    "a module constant read through a local flips": (
        "SKIP = False\n\ndef test_b():\n    flag = SKIP\n    if flag:\n        pytest.skip('r')\n",
        "SKIP = True\n\ndef test_b():\n    flag = SKIP\n    if flag:\n        pytest.skip('r')\n",
    ),
    "a statement beside the skip changes order": (
        "def test_b(items):\n    items.sort()\n    pytest.skip('r')\n",
        "def test_b(items):\n    pytest.skip('r')\n    items.sort()\n",
    ),
    "an earlier binding of a name that is bound again later changes": (
        "SKIP = False\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n\nSKIP = False\n",
        "SKIP = True\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n\nSKIP = False\n",
    ),
    "a list that guards a skipif is mutated before it": (
        "FLAGS = []\n\n@pytest.mark.skipif(FLAGS, reason='r')\ndef test_b():\n    pass\n",
        "FLAGS = []\nFLAGS.append(True)\n\n@pytest.mark.skipif(FLAGS, reason='r')\ndef test_b():\n    pass\n",
    ),
    "a tuple assignment flips": (
        "SKIP, LIMIT = False, 10\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
        "SKIP, LIMIT = True, 10\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "a chained assignment flips": (
        "SKIP = OTHER = False\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
        "SKIP = OTHER = True\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "an augmented module assignment flips": (
        "SKIP = False\nSKIP |= False\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
        "SKIP = False\nSKIP |= True\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "a module-level loop over a constant gains a member": (
        "OPTIONAL = []\nfor package in OPTIONAL:\n    pytest.importorskip(package)\n",
        "OPTIONAL = ['missing_package']\nfor package in OPTIONAL:\n    pytest.importorskip(package)\n",
    ),
    "a guarded import at module level changes": (
        "try:\n    import vcr\nexcept ImportError:\n    pytest.skip('r', allow_module_level=True)\n",
        "try:\n    import vcr_missing\nexcept ImportError:\n    pytest.skip('r', allow_module_level=True)\n",
    ),
    "a class-level guard changes": (
        "class TestK:\n    if False:\n        pytestmark = pytest.mark.skip\n",
        "class TestK:\n    if True:\n        pytestmark = pytest.mark.skip\n",
    ),
    "an imported guard constant is re-pointed at another module": (
        "from helpers import SKIP\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
        "from other_helpers import SKIP\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "an imported module that holds the guard is re-pointed": (
        "import helpers\n\n@pytest.mark.skipif(helpers.SKIP, reason='r')\ndef test_b():\n    pass\n",
        "import other_helpers as helpers\n\n@pytest.mark.skipif(helpers.SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "mutations of a guard list are reordered": (
        "FLAGS = []\nFLAGS.append(True)\nFLAGS.clear()\n\n@pytest.mark.skipif(FLAGS, reason='r')\ndef test_b():\n    pass\n",
        "FLAGS = []\nFLAGS.clear()\nFLAGS.append(True)\n\n@pytest.mark.skipif(FLAGS, reason='r')\ndef test_b():\n    pass\n",
    ),
    "class-level mutations of a guard are reordered": (
        "class TestK:\n    FLAGS = []\n    FLAGS.append(True)\n    FLAGS.clear()\n\n    def test_b(self):\n        if self.FLAGS:\n            pytest.skip('r')\n",
        "class TestK:\n    FLAGS = []\n    FLAGS.clear()\n    FLAGS.append(True)\n\n    def test_b(self):\n        if self.FLAGS:\n            pytest.skip('r')\n",
    ),
    "a helper that computes the guard changes": (
        "def should_skip():\n    return False\n\n@pytest.mark.skipif(should_skip(), reason='r')\ndef test_b():\n    pass\n",
        "def should_skip():\n    return True\n\n@pytest.mark.skipif(should_skip(), reason='r')\ndef test_b():\n    pass\n",
    ),
    "a constant a helper reads flips": (
        "LIMIT = 1\n\ndef should_skip():\n    return LIMIT > 5\n\n@pytest.mark.skipif(should_skip(), reason='r')\ndef test_b():\n    pass\n",
        "LIMIT = 9\n\ndef should_skip():\n    return LIMIT > 5\n\n@pytest.mark.skipif(should_skip(), reason='r')\ndef test_b():\n    pass\n",
    ),
    "a helper that calls another helper changes": (
        "def inner():\n    return False\n\ndef outer():\n    return inner()\n\n@pytest.mark.skipif(outer(), reason='r')\ndef test_b():\n    pass\n",
        "def inner():\n    return True\n\ndef outer():\n    return inner()\n\n@pytest.mark.skipif(outer(), reason='r')\ndef test_b():\n    pass\n",
    ),
    "a method helper a skip calls through self changes": (
        "class TestK:\n    def guard(self):\n        return False\n\n    def test_b(self):\n        if self.guard():\n            pytest.skip('r')\n",
        "class TestK:\n    def guard(self):\n        return True\n\n    def test_b(self):\n        if self.guard():\n            pytest.skip('r')\n",
    ),
    "a string skipif condition reads a constant that flips": (
        "SKIP = False\n\n@pytest.mark.skipif('SKIP', reason='r')\ndef test_b():\n    pass\n",
        "SKIP = True\n\n@pytest.mark.skipif('SKIP', reason='r')\ndef test_b():\n    pass\n",
    ),
    "an inherited class attribute flips": (
        "class Base:\n    DISABLED = False\n\nclass TestK(Base):\n    def test_a(self):\n        if self.DISABLED:\n            pytest.skip('r')\n",
        "class Base:\n    DISABLED = True\n\nclass TestK(Base):\n    def test_a(self):\n        if self.DISABLED:\n            pytest.skip('r')\n",
    ),
    "a decorated test moves below a rebinding": (
        "SKIP = False\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n\nSKIP = True\n",
        "SKIP = False\nSKIP = True\n\n@pytest.mark.skipif(SKIP, reason='r')\ndef test_b():\n    pass\n",
    ),
    "an unrelated test beside a skipping one changes": (
        "def test_b(flag=False):\n    if flag:\n        pytest.skip('r')\n\ndef test_c():\n    assert 1\n",
        "def test_b(flag=False):\n    if flag:\n        pytest.skip('r')\n\ndef test_c():\n    assert 2\n",
    ),
    "a keyword-only default flips": (
        "def test_b(*, flag=False):\n    if flag:\n        pytest.skip('r')\n",
        "def test_b(*, flag=True):\n    if flag:\n        pytest.skip('r')\n",
    ),
    "a class attribute flips": (
        "class TestK:\n    SKIP = False\n\n    def test_b(self):\n        if self.SKIP:\n            pytest.skip('r')\n",
        "class TestK:\n    SKIP = True\n\n    def test_b(self):\n        if self.SKIP:\n            pytest.skip('r')\n",
    ),
    "a local read through another local flips": (
        "def test_b():\n    base = False\n    skip_test = base\n    if skip_test:\n        pytest.skip('r')\n",
        "def test_b():\n    base = True\n    skip_test = base\n    if skip_test:\n        pytest.skip('r')\n",
    ),
    "a while guard changes": (
        "def test_b():\n    while False:\n        pytest.skip('r')\n",
        "def test_b():\n    while True:\n        pytest.skip('r')\n",
    ),
    "a ternary that guards a skip changes": (
        "def test_b():\n    (pytest.skip('r') if False else None)\n",
        "def test_b():\n    (pytest.skip('r') if True else None)\n",
    ),
}


@pytest.mark.parametrize("name", sorted(CONDITION_EDITS))
def test_changing_the_condition_a_skip_runs_under_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    before, after = CONDITION_EDITS[name]
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST + "\n\n" + after},
        base=base_files() | {"tests/test_a.py": CLEAN_TEST + "\n\n" + before},
    )
    code, output = check(repo, capsys)
    assert code == 1, output


def test_a_condition_around_an_unchanged_skip_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "def test_b(flag):\n    if flag:\n        pytest.skip('r')\n"
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST + "\n\n" + text, "README.md": "y\n"},
        base=base_files() | {"tests/test_a.py": CLEAN_TEST + "\n\n" + text},
    )
    assert check(repo, capsys)[0] == 0


RUNTIME_MARKERS = {
    "autouse fixture adds a skip marker": (
        "@pytest.fixture(autouse=True)\ndef _no(request):\n    request.node.add_marker('skip')\n"
    ),
    "autouse fixture applies an xfail marker": (
        "@pytest.fixture(autouse=True)\ndef _no(request):\n    request.applymarker('xfail')\n"
    ),
    "a hook adds a marker object": (
        "def helper(item):\n    item.add_marker(pytest.mark.usefixtures('x'))\n"
    ),
    "the markexpr option is set": "def helper(config):\n    config.option.markexpr = 'not timing'\n",
    "the keyword option is set": "def helper(config):\n    config.option.keyword = 'nothing'\n",
}


@pytest.mark.parametrize("name", sorted(RUNTIME_MARKERS))
def test_a_marker_or_selection_option_applied_at_run_time_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, {"tests/test_a.py": CLEAN_TEST + "\n\n" + RUNTIME_MARKERS[name]})
    code, output = check(repo, capsys)
    assert code == 1, output


HOOK_CONSTANT_EDITS = {
    "a constant a hook returns": (
        "IGNORE = False\n\ndef pytest_ignore_collect(collection_path):\n    return IGNORE\n",
        "IGNORE = True\n\ndef pytest_ignore_collect(collection_path):\n    return IGNORE\n",
    ),
    "a constant a hook uses to drop items": (
        "DROP = []\n\ndef pytest_collection_modifyitems(items):\n    items[:] = [i for i in items if i not in DROP]\n",
        "DROP = ['x']\n\ndef pytest_collection_modifyitems(items):\n    items[:] = [i for i in items if i not in DROP]\n",
    ),
}


@pytest.mark.parametrize("name", sorted(HOOK_CONSTANT_EDITS))
def test_changing_a_constant_a_collection_hook_reads_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    before, after = HOOK_CONSTANT_EDITS[name]
    repo = scenario(
        tmp_path, {"tests/conftest.py": after}, base=base_files() | {"tests/conftest.py": before}
    )
    code, output = check(repo, capsys)
    assert code == 1, output


INSIDE_A_SKIPPING_FUNCTION = {
    "an unrelated local": (
        "def test_b():\n    limit = 1\n    flag = False\n    if flag:\n        pytest.skip('r')\n    assert limit\n",
        "def test_b():\n    limit = 2\n    flag = False\n    if flag:\n        pytest.skip('r')\n    assert limit\n",
    ),
    "an unrelated default": (
        "def test_b(limit=1, flag=False):\n    if flag:\n        pytest.skip('r')\n    assert limit\n",
        "def test_b(limit=2, flag=False):\n    if flag:\n        pytest.skip('r')\n    assert limit\n",
    ),
    "an unrelated assertion": (
        "def test_b(flag=False):\n    if flag:\n        pytest.skip('r')\n    assert 1\n",
        "def test_b(flag=False):\n    if flag:\n        pytest.skip('r')\n    assert 2\n",
    ),
}


@pytest.mark.parametrize("name", sorted(INSIDE_A_SKIPPING_FUNCTION))
def test_any_edit_inside_a_file_that_skips_is_reported_for_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """A file with a skip-like construct is compared whole: erring towards a finding."""
    before, after = INSIDE_A_SKIPPING_FUNCTION[name]
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST + "\n\n" + after},
        base=base_files() | {"tests/test_a.py": CLEAN_TEST + "\n\n" + before},
    )
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "tests/test_a.py" in output


# --- shard: leader skips a gate in unsharded runs; making an existing gate one is a finding ---

SPLIT_BASE: dict[str, Any] = {
    **BASE_CONTRACT,
    "gates": [
        *[dict(g) for g in BASE_CONTRACT["gates"]],
        {
            "id": "unit_narrow",
            "phase": "unit_full",
            "classify": "test",
            "command": ["pytest", "tests/", "-q", "--tb=short", "-m", "narrow"],
        },
    ],
}


def _split_base() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(yaml.safe_dump(SPLIT_BASE))
    _gate(contract, "unit_full")["shard"] = "split"
    return contract


def test_an_existing_gate_made_a_leader_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _split_base()
    _gate(head, "unit_narrow")["shard"] = "leader"
    repo = scenario(
        tmp_path,
        {"proof/contract.yaml": _contract_text(head)},
        base=base_files() | {"proof/contract.yaml": _contract_text(_split_base())},
    )
    code, output = check(repo, capsys)
    assert code == 1, output
    assert "unit_narrow" in output and "leader" in output


def test_a_new_leader_gate_and_an_existing_leader_staying_one_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _split_base()
    _gate(base, "unit_narrow")["shard"] = "leader"
    head = yaml.safe_load(yaml.safe_dump(base))
    head["gates"].append(
        {
            "id": "unit_more",
            "phase": "unit_full",
            "classify": "test",
            "command": ["pytest", "tests/", "-q", "--tb=short", "-m", "more"],
            "shard": "leader",
        }
    )
    repo = scenario(
        tmp_path,
        {"proof/contract.yaml": _contract_text(head)},
        base=base_files() | {"proof/contract.yaml": _contract_text(base)},
    )
    code, output = check(repo, capsys)
    assert code == 0, output


SELECTION_MUTATIONS = {
    "an ignore entry is appended": "def pytest_configure(config):\n    config.option.ignore.append('tests/test_a.py')\n",
    "a deselect list is extended": "def helper(config):\n    config.option.deselect.extend(['tests/test_a.py::test_a'])\n",
    "an ignore_glob entry is inserted": "def helper(config):\n    config.option.ignore_glob.insert(0, '*.py')\n",
    "a selection option is updated in place": "def helper(option):\n    option.keyword.update({'x': 1})\n",
    "an entry of a selection option is replaced": "def helper(config):\n    config.option.ignore[0] = 'tests/'\n",
}


@pytest.mark.parametrize("name", sorted(SELECTION_MUTATIONS))
def test_changing_a_pytest_selection_option_in_place_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = scenario(tmp_path, {"tests/conftest.py": SELECTION_MUTATIONS[name]}, base=base_files())
    code, output = check(repo, capsys)
    assert code == 1, output


NO_CONSTRUCTS_EDITS = {
    "a helper and a constant": (
        "LIMIT = 1\n\ndef helper():\n    return LIMIT\n\ndef test_b():\n    assert helper()\n",
        "LIMIT = 2\n\ndef helper():\n    return LIMIT + 1\n\ndef test_b():\n    assert helper()\n",
    ),
    "an assertion and a parametrize": (
        "@pytest.mark.parametrize('v', [1])\ndef test_b(v):\n    assert v\n",
        "@pytest.mark.parametrize('v', [1, 2])\ndef test_b(v):\n    assert v == 1 or v == 2\n",
    ),
}


@pytest.mark.parametrize("name", sorted(NO_CONSTRUCTS_EDITS))
def test_any_edit_to_a_file_with_no_skip_like_construct_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    before, after = NO_CONSTRUCTS_EDITS[name]
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST + "\n\n" + after},
        base=base_files() | {"tests/test_a.py": CLEAN_TEST + "\n\n" + before},
    )
    assert check(repo, capsys)[0] == 0


def test_an_edit_in_a_file_that_gains_a_skip_is_reported_as_a_whole_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = "def test_b():\n    assert 1\n"
    after = "def test_b():\n    assert 2\n    pytest.skip('r')\n"
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": CLEAN_TEST + "\n\n" + after},
        base=base_files() | {"tests/test_a.py": CLEAN_TEST + "\n\n" + before},
    )
    code, output = check(repo, capsys)
    assert code == 1
    assert "file with skip-like constructs changed" in output


def test_reformatting_and_docstring_edits_in_a_file_that_skips_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (
        '"""Old module words."""\nimport pytest\n\n\ndef test_b(flag=False):\n    """Old."""\n'
        "    if flag:\n        pytest.skip('r')\n"
    )
    after = (
        '"""New, longer module words."""\nimport pytest\n\n\n\ndef test_b(flag = False):\n    """New."""\n'
        '    if flag:\n        pytest.skip( "r" )  # a comment\n'
    )
    repo = scenario(
        tmp_path, {"tests/test_a.py": after}, base=base_files() | {"tests/test_a.py": before}
    )
    code, output = check(repo, capsys)
    assert code == 0, output
