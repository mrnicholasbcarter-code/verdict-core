"""The quick start must work for someone who is not the operator (issue #1413).

``scripts/quickstart.sh`` once hard-coded one developer's checkout path, so it could not run
from any other clone. README Quick start step 3 once listed four needs for a live run and left
out the ones the default worker executor adds, so a stranger following it was blocked.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "quickstart.sh"
README = REPO_ROOT / "README.md"

# A path that belongs to one machine: a home directory or a user's checkout.
_HOST_PATH = re.compile(r"/(?:home|Users|root)/|~/dev/")


def _step_3(readme: str) -> str:
    start = readme.index("### Step 3")
    end = readme.index("\n## ", start)
    return readme[start:end]


def test_quickstart_script_contains_no_host_specific_path() -> None:
    hits = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(), start=1)
        if _HOST_PATH.search(line)
    ]
    assert not hits, (
        "scripts/quickstart.sh names a path that belongs to one machine:\n" + "\n".join(hits)
    )


def test_quickstart_script_resolves_the_repo_root_from_its_own_location() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(
        r'REPO_ROOT="\$\(cd "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)/\.\." && pwd\)"', text
    )
    # The root it resolved is the only thing the script installs from.
    assert 'pip install -e "$REPO_ROOT"' in text


def test_quickstart_script_keeps_its_state_out_of_the_real_home() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'export VERDICT_HOME="$QUICKSTART_DIR/' in text


def test_quickstart_script_runs_the_two_offline_readme_steps() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    for command in ("verdict quickstart --non-interactive --dry-run", "verdict demo"):
        assert f"\n{command}\n" in text
        assert command in readme


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_quickstart_script_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_readme_step_3_names_everything_a_default_live_run_needs() -> None:
    step = _step_3(README.read_text(encoding="utf-8"))
    for needed in (
        "http://localhost:20128",
        "VERDICT_OMNIROUTE_API_KEY",
        "`ocr`",
        "git repository",
        "`opencode`",
        "verdict serve",
        "signing key",
        "verdict setup --apply --allow host.serve",
        "--executor direct-gateway",
    ):
        assert needed in step, f"README step 3 does not mention {needed!r}"


def test_readme_step_3_says_doctor_is_red_until_setup_has_run() -> None:
    step = _step_3(README.read_text(encoding="utf-8"))
    assert re.search(r"`verdict doctor`[^.\n]*(?:red|exits 1)[^.\n]*`verdict setup`", step)


def test_readme_step_3_says_which_steps_before_it_need_nothing() -> None:
    step = _step_3(README.read_text(encoding="utf-8"))
    assert "Steps 1 and 2" in step
    assert re.search(r"Steps 1 and 2[^.\n]*(?:no gateway|nothing)", step)
