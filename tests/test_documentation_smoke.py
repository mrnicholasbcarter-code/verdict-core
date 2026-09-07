"""CI-runnable smoke coverage for the documented offline user journey."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from verdict import cli

JOURNEY_COMMANDS = (
    "verdict --help",
    "verdict detect --offline --json",
    "verdict quickstart --non-interactive --dry-run --json",
    "verdict autodev-golden-path",
    "verdict failover-proof",
    "verdict replay",
)
README_REQUIRED_ORDER = (
    "The LLM router that says no",
    "## Install",
    "## Quick start",
    "Receipt: fixture:issue-35 (deterministic_fixture)",
    "## Cost comparison",
    "$0.16 routed",
    "$0.52 baseline",
    "## Architecture",
)


def _markdown_slug(heading: str) -> str:
    value = heading.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value)
    return re.sub(r"[\s-]+", "-", value).strip("-")


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["NO_COLOR"] = "1"
    for name in tuple(env):
        if name.endswith("_API_KEY") or name in {
            "AWS_ACCESS_KEY_ID",
            "CLAUDE_API_KEY",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "HF_TOKEN",
            "HUGGINGFACE_HUB_TOKEN",
            "REPLICATE_API_TOKEN",
        }:
            env.pop(name)
    return env


def _run_cli(
    *args: str, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "verdict.cli", *args),
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path, env: dict[str, str]) -> None:
    repo.mkdir()
    subprocess.run(("git", "init", "-q", str(repo)), env=env, check=True)
    subprocess.run(
        ("git", "-C", str(repo), "config", "user.email", "smoke@example.invalid"),
        env=env,
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repo), "config", "user.name", "Verdict Smoke"), env=env, check=True
    )
    (repo / "README.md").write_text("# smoke fixture\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repo), "add", "README.md"), env=env, check=True)
    subprocess.run(
        ("git", "-C", str(repo), "commit", "-q", "-m", "smoke fixture"), env=env, check=True
    )


def test_documented_commands_are_present_and_maturity_is_truthful() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    journey = Path("docs/USER_JOURNEY.md").read_text(encoding="utf-8")

    for command in JOURNEY_COMMANDS:
        assert command in readme or command in journey
    for status in (
        "production functional",
        "functional but incomplete",
        "simulated only",
        "missing",
    ):
        assert status in journey
    assert "3500+ models" not in readme
    assert "OMNIROUTE (Intelligent Model Router)" not in readme
    assert "Deterministic mock — no provider spend." in readme
    assert "These are estimates, not observed invoices." in readme
    assert "Context packing — dated live observation, not offline proof." in readme
    assert "A blocked or skipped live run makes no lift claim." in readme
    assert "Deterministic mock — no provider spend." in readme
    assert "These are estimates, not observed invoices." in readme
    assert "Context packing — dated live observation, not offline proof." in readme
    assert "A blocked or skipped live run makes no lift claim." in readme


def test_readme_recruiter_path_is_ordered_and_local_links_resolve() -> None:
    readme_path = Path("README.md")
    readme = readme_path.read_text(encoding="utf-8")

    offsets = [readme.index(marker) for marker in README_REQUIRED_ORDER]
    assert offsets == sorted(offsets)

    install_section = readme.split("## Install", 1)[1].split("## Quick start", 1)[0]
    proof_section = readme.split("## Quick start", 1)[1].split("## Live gateway checks", 1)[0]
    assert install_section.count("```bash") == 1
    assert "pip install verdict-core" in install_section
    assert "verdict quickstart --non-interactive --dry-run" in proof_section

    markdown_links = re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", readme)
    for destination in markdown_links:
        if destination.startswith(("http://", "https://", "mailto:")):
            continue
        target_name, _, fragment = destination.partition("#")
        target = readme_path if not target_name else readme_path.parent / unquote(target_name)
        assert target.exists(), f"README link target does not exist: {destination}"
        if fragment:
            assert target.is_file(), f"README fragment target is not a file: {destination}"
            target_text = target.read_text(encoding="utf-8")
            headings = {
                _markdown_slug(match.group(1))
                for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", target_text, re.MULTILINE)
            }
            assert unquote(fragment).lower() in headings, (
                f"README link fragment does not exist: {destination}"
            )


def test_detect_offline_does_not_use_discovery_seams(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import verdict.provider_detection as provider_detection

    def unexpected_discovery() -> None:
        raise AssertionError("offline detection must not inspect providers")

    monkeypatch.setattr(provider_detection, "detect_all_providers", unexpected_discovery)
    cli.cmd_detect(output_json=True, offline=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "offline"
    assert payload["network_access"] is False
    assert payload["credentials_read"] is False
    assert payload["local_providers"] == []


def test_documented_offline_user_journey_runs_end_to_end(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    repo = tmp_path / "mission-repo"
    memory_path = tmp_path / "journey-memory.db"
    _init_repo(repo, env)

    help_result = _run_cli("--help", env=env)
    for command in ("detect", "quickstart", "autodev-golden-path", "failover-proof", "replay"):
        assert command in help_result.stdout

    detection = json.loads(_run_cli("detect", "--offline", "--json", env=env).stdout)
    assert detection["mode"] == "offline"
    assert detection["network_access"] is False

    route = json.loads(
        _run_cli("quickstart", "--non-interactive", "--dry-run", "--json", env=env).stdout
    )
    assert route["decision"]["selected_route"]["runtime_id"] == "demo/frontier-tools"
    assert route["decision"]["exclusions"]

    mission = json.loads(
        _run_cli(
            "autodev-golden-path",
            "--objective",
            "verify a small repository",
            "--repo",
            str(repo),
            "--memory-path",
            str(tmp_path / "golden-memory.db"),
            "--json",
            env=env,
        ).stdout
    )
    assert mission["decision"] == "accepted"
    assert [stage["stage"] for stage in mission["stages"]] == [
        "discovery",
        "memory",
        "verification",
    ]

    failover = json.loads(
        _run_cli("failover-proof", "--memory-path", str(memory_path), "--json", env=env).stdout
    )
    assert failover["failure_status"] == 429
    assert failover["replacement_model"] != failover["initial_model"]
    repeated_failover = json.loads(
        _run_cli("failover-proof", "--memory-path", str(memory_path), "--json", env=env).stdout
    )
    assert repeated_failover["session_id"] != failover["session_id"]
    assert repeated_failover["completed_steps"] == failover["completed_steps"]

    replay_env = {**env, "VERDICT_MEMORY_DB": str(memory_path)}
    replay = json.loads(_run_cli("replay", failover["session_id"], "--json", env=replay_env).stdout)
    assert replay["session_id"] == failover["session_id"]
    assert replay["completed_steps"] == failover["completed_steps"]
