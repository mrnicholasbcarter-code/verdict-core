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


def test_unrelated_imports_and_unchanged_aliases_still_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = CLEAN_TEST + "\n\n" + TWO_SCOPES.format(a="skip", b="xfail")
    repo = scenario(
        tmp_path,
        {"tests/test_a.py": text + "\nimport os\nfrom pytest import fixture, approx\n"},
        base=base_files() | {"tests/test_a.py": text},
    )
    assert check(repo, capsys)[0] == 0
