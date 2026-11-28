"""Offline acceptance checks for the tracked story-review shell tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def review_tools(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    tools = tmp_path / ".claude" / "tools"
    tools.mkdir(parents=True)
    state = tmp_path / "state"
    (state / "reviews").mkdir(parents=True)
    for name in ("review_pr.sh", "verify_ready.sh"):
        source = (REPO / ".claude" / "tools" / name).read_text()
        (tools / name).write_text(source.replace("/tmp/claude-takeover", str(state)))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    stub = bin_dir / "stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, pathlib, shutil, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
if name == "git":
    if "rev-parse" in args:
        print("a" * 40)
    elif "diff" in args:
        print("changed.py")
    elif "worktree" in args:
        if "add" in args:
            pathlib.Path(args[-2]).mkdir(parents=True)
        elif "remove" in args:
            shutil.rmtree(args[-1])
elif name == "gh":
    if "headRefOid" in args:
        print("a" * 40)
    elif "state" in args:
        print("OPEN")
    else:
        print(json.dumps({"check_runs": [{"name": "lint", "status": "completed", "conclusion": "success"}]}))
else:
    if args[:2] == ["-I", "-"]:
        sys.exit(0 if os.environ.get("STUB_EXHAUSTED") == "1" else 1)
    model, out = args[4:6]
    with open(os.environ["STUB_CALLS"], "a") as stream:
        stream.write(model + "\\n")
    pathlib.Path(out).write_text(json.dumps({
        "status": "complete", "llm": {"model": model}, "comments": [],
        "manifest": {"coverage": {"selected": ["changed.py"], "completed": ["changed.py"]}},
        "tool_calls": {"total": 1, "failure": 0},
    }))
"""
    )
    stub.chmod(0o755)
    for name in ("git", "gh", "review-python"):
        (bin_dir / name).symlink_to(stub)
    environment = {
        "HOME": str(tmp_path),
        "VERDICT_HOME": str(tmp_path / ".verdict"),
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "VERDICT_PYTHON": str(bin_dir / "review-python"),
        "STUB_CALLS": str(calls),
    }
    # python3 in the shell script is real; only the review/probe interpreter is stubbed.
    python3 = shutil.which("python3")
    assert python3
    (bin_dir / "python3").symlink_to(python3)
    return tools, environment, calls


def run_review(
    setup: tuple[Path, dict[str, str], Path], *models: str, **settings: str
) -> subprocess.CompletedProcess[str]:
    tools, environment, _ = setup
    result = subprocess.run(
        ["bash", str(tools / "review_pr.sh"), str(tools.parents[1]), "offline acceptance", *models],
        env={**environment, **settings},
        capture_output=True,
        text=True,
        check=False,
    )
    print(result.stdout.strip())
    print(f"EXIT={result.returncode}")
    return result


@pytest.mark.parametrize(
    ("models", "settings", "expected"),
    [
        ((), {}, "cc/claude-sonnet-5-5"),
        ((), {"REVIEW_MODEL": "agy/gemini-custom"}, "agy/gemini-custom"),
        (("agy/gemini-explicit",), {"REVIEW_MODEL": "cc/ignored"}, "agy/gemini-explicit"),
    ],
    ids=["default", "environment", "explicit-wins"],
)
def test_reviewer_selection(review_tools, models, settings, expected):
    result = run_review(review_tools, *models, REVIEW_AUTHOR_FAMILY="codex", **settings)
    assert result.returncode == 0, result.stderr
    assert review_tools[2].read_text().splitlines() == [expected]
    assert f"model={expected}" in result.stdout
    assert "reviewer_family=" in result.stdout


@pytest.mark.parametrize(
    ("model", "author"),
    [
        ("cx/gpt-6.1-sol", "codex"),
        ("codex/other", "codex"),
        ("CX/gpt-6.1-sol", "Codex"),
        ("cc/claude-sonnet-5-5", "claude"),
        ("claude/other", "claude"),
        ("agy/gemini-other", "gemini"),
        ("antigravity/gemini-other", "gemini"),
    ],
)
def test_same_family_is_not_obtained(review_tools, model, author):
    result = run_review(review_tools, model, REVIEW_AUTHOR_FAMILY=author)
    assert result.returncode != 0
    assert "same family" in result.stdout
    assert "NOT a pass" in result.stdout
    assert not review_tools[2].exists()


def test_author_family_fallback_is_skipped(review_tools):
    result = run_review(
        review_tools,
        REVIEW_AUTHOR_FAMILY="codex",
        STUB_EXHAUSTED="1",
        REVIEW_FALLBACK_MODELS="cx/gpt-6.1-sol agy/gemini-safe",
    )
    assert result.returncode == 0, result.stderr
    assert review_tools[2].read_text().splitlines() == ["agy/gemini-safe"]
    assert "same family" in result.stdout
    assert "skipped" in result.stdout
    assert "model=agy/gemini-safe" in result.stdout


def test_no_allowed_fallback_is_not_obtained(review_tools):
    result = run_review(
        review_tools,
        REVIEW_AUTHOR_FAMILY="codex",
        STUB_EXHAUSTED="1",
        REVIEW_FALLBACK_MODELS="cx/gpt-6.1-sol codex/other",
    )
    assert result.returncode != 0
    assert "same family" in result.stdout
    assert "NOT a pass" in result.stdout
    assert not review_tools[2].exists()


def test_unknown_family_is_named_and_treated_as_different(review_tools):
    result = run_review(review_tools, "custom/model", REVIEW_AUTHOR_FAMILY="codex")
    assert result.returncode == 0, result.stderr
    assert "unknown:custom" in result.stdout
    assert "treated as different" in result.stdout


def test_cached_same_family_review_is_not_reused(review_tools):
    result = run_review(review_tools, "cx/gpt-6.1-sol")
    assert result.returncode == 0
    review_tools[2].unlink()
    result = run_review(review_tools, REVIEW_AUTHOR_FAMILY="codex")
    assert result.returncode == 0, result.stderr
    assert "(stored result for unchanged head)" not in result.stdout
    assert review_tools[2].read_text().splitlines() == ["cc/claude-sonnet-5-5"]


def test_invalid_author_family_is_not_obtained(review_tools):
    result = run_review(review_tools, REVIEW_AUTHOR_FAMILY="openai")
    assert result.returncode != 0
    assert "invalid REVIEW_AUTHOR_FAMILY" in result.stdout
    assert "NOT a pass" in result.stdout
    assert not review_tools[2].exists()


def test_malformed_record_is_not_ready(review_tools):
    tools, environment, _ = review_tools
    record = tools.parents[1] / "state" / "reviews" / ("a" * 40 + ".independent.json")
    record.write_text("{")
    result = subprocess.run(
        ["bash", str(tools / "verify_ready.sh"), "1427"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.startswith("NOT READY ")
    assert "unreadable review record" in result.stdout
    assert "Traceback" not in result.stderr


def test_missing_reviewer_identity_is_not_a_cached_pass(review_tools):
    tools, _, _ = review_tools
    result = run_review(review_tools)
    assert result.returncode == 0
    record = tools.parents[1] / "state" / "reviews" / ("a" * 40 + ".json")
    data = json.loads(record.read_text())
    data["llm"] = {}
    record.write_text(json.dumps(data))
    review_tools[2].unlink()
    result = run_review(review_tools, REVIEW_AUTHOR_FAMILY="codex")
    assert result.returncode == 0, result.stderr
    assert "(stored result for unchanged head)" not in result.stdout
    assert review_tools[2].read_text().splitlines() == ["cc/claude-sonnet-5-5"]


def test_readiness_accepts_new_default_not_old_default(review_tools):
    tools, environment, _ = review_tools
    for model, expected in [("cc/claude-sonnet-5-5", 0), ("cx/gpt-6.1-sol", 1)]:
        record = {
            "status": "complete",
            "llm": {"model": model},
            "manifest": {"coverage": {"selected": ["changed.py"], "completed": ["changed.py"]}},
        }
        path = tools.parents[1] / "state" / "reviews" / ("a" * 40 + ".independent.json")
        path.write_text(json.dumps(record))
        result = subprocess.run(
            ["bash", str(tools / "verify_ready.sh"), "1427"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr
