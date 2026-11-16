"""The executable definition of done (scripts/check_dod.py) rejects false "done"."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_URL = "https://github.com/owner/repo/actions/runs/123"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dod", ROOT / "scripts" / "check_dod.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_dod = _load()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _artifact(root: Path, name: str, text: str) -> dict[str, str]:
    path = root / "docs/proof/dod" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"path": f"docs/proof/dod/{name}", "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _run(
    root: Path, sha: str, at: str, name: str, command: str = "gh run view 123"
) -> dict[str, Any]:
    return {
        "at": at,
        "sha": sha,
        "environment": "github-actions",
        "command": command,
        "real_state_marker": RUN_URL,
        "exit_code": 0,
        "raw_output": _artifact(
            root, name, f"{RUN_URL}\ndrill: orphan module FAILED as intended\n{name}\n"
        ),
    }


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "repo"
    archived = root / "openspec/changes/archive/2026-10-13-gate"
    archived.mkdir(parents=True)
    (archived / "tasks.md").write_text("# Tasks\n- [x] build it\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts/tool.py").write_text("print(1)\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "gate")
    sha = _git(root, "rev-parse", "HEAD")
    evidence = {
        "schema_version": 1,
        "story": "gate",
        "kind": "repo-tooling",
        "author": "worker-a",
        "merged_sha": sha,
        "pr": 1,
        "on_main": _run(root, sha, "2099-01-01T00:00:00Z", "main.txt", "gh pr checks 1 --required"),
        "acceptance": [
            {
                "criterion": "an orphan module fails CI",
                "expect": "FAILED as intended",
                "first_run": _run(root, sha, "2099-01-01T00:00:00Z", "first.txt"),
                "fresh_run": _run(root, sha, "2099-01-02T01:00:00Z", "fresh.txt"),
            }
        ],
        "freshness_window_hours": 24,
        "stays_true_job": "gate-drills.yml (daily)",
        "setup_doctor": {"status": "not_applicable", "reason": "repo tooling, no service"},
        "openspec": {"change": "gate"},
        "parent_confirmation": {
            "by": "parent",
            "at": "2099-01-02T02:00:00Z",
            "command": "gh run view 123",
            "raw_output": _artifact(
                root, "confirm.txt", "drill: orphan module FAILED as intended\n"
            ),
        },
    }
    return root, evidence


def _check(root: Path, evidence: dict[str, Any]) -> list[str]:
    path = root / "docs/proof/dod/gate.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return check_dod.check_evidence(root, path)


def test_complete_evidence_is_done(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    assert _check(root, evidence) == []


@pytest.mark.parametrize(
    "field", sorted(json.loads((ROOT / "proof/dod.schema.json").read_text())["required"])
)
def test_each_missing_field_fails(repo: tuple[Path, dict[str, Any]], field: str) -> None:
    root, evidence = repo
    del evidence[field]
    assert any(field in error for error in _check(root, evidence))


def _mutate(evidence: dict[str, Any], dotted: str, value: object) -> dict[str, Any]:
    changed = copy.deepcopy(evidence)
    node: Any = changed
    *parents, leaf = dotted.split(".")
    for part in parents:
        node = node[int(part)] if part.isdigit() else node[part]
    node[leaf] = value
    return changed


@pytest.mark.parametrize(
    ("dotted", "value", "reason"),
    [
        ("merged_sha", "0" * 40, "not a commit"),
        ("acceptance.0.first_run.sha", "1" * 40, "not the merged sha or a later commit"),
        ("acceptance.0.first_run.raw_output.sha256", "0" * 64, "does not match its sha256"),
        ("acceptance.0.first_run.real_state_marker", "https://example.invalid/x", "run URL"),
        ("acceptance.0.expect", "something else", "expected text"),
        ("acceptance.0.fresh_run.at", "2099-01-01T12:00:00Z", "freshness window"),
        ("acceptance.0.fresh_run.command", "gh run view 999", "command differs"),
        ("acceptance.0.first_run.exit_code", 1, "exit code 1"),
        ("acceptance.0.first_run.at", "2000-01-01T00:00:00Z", "before the merge"),
        ("on_main.exit_code", 1, "required checks not green"),
        ("parent_confirmation.by", "worker-a", "cannot confirm their own"),
        ("parent_confirmation.command", "echo ok", "not one of the acceptance commands"),
        ("openspec.change", "other", "not in openspec/changes/archive"),
        ("kind", "product", "operator host"),
        ("story", "other", "does not match the file name"),
        ("parent_confirmation.raw_output.path", "docs/proof/dod/../../../etc/passwd", "outside"),
    ],
)
def test_mismatched_field_fails(
    repo: tuple[Path, dict[str, Any]], dotted: str, value: object, reason: str
) -> None:
    root, evidence = repo
    errors = _check(root, _mutate(evidence, dotted, value))
    assert any(reason in error for error in errors), errors


def test_unticked_task_and_unarchived_change_fail(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    tasks = root / "openspec/changes/archive/2026-10-13-gate/tasks.md"
    tasks.write_text("# Tasks\n- [ ] not done\n", encoding="utf-8")
    assert any("unticked" in e for e in _check(root, evidence))
    (root / "openspec/changes/gate").mkdir(parents=True)
    assert any("not archived" in e for e in _check(root, evidence))


OPEN_POST_MERGE = "# Tasks\n- [x] build it\n- [ ] (post-merge) run the live check on merged main\n"


def test_open_post_merge_task_is_not_done(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    archived = root / "openspec/changes/archive/2026-10-13-gate"
    (archived / "tasks.md").write_text(OPEN_POST_MERGE, encoding="utf-8")
    errors = _check(root, evidence)
    assert "openspec change gate has unticked tasks" in errors, errors
    shutil.move(archived, root / "openspec/changes/gate")
    errors = _check(root, evidence)
    assert "openspec change gate is not archived" in errors, errors


@pytest.mark.parametrize("with_evidence", [True, False])
def test_archive_with_open_post_merge_task_fails_all(
    repo: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str], with_evidence: bool
) -> None:
    root, evidence = repo
    tasks = root / "openspec/changes/archive/2026-10-13-gate/tasks.md"
    tasks.write_text(OPEN_POST_MERGE, encoding="utf-8")
    if with_evidence:
        (root / "docs/proof/dod/gate.json").write_text(json.dumps(evidence), encoding="utf-8")
    assert check_dod.main(["--all", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    if with_evidence:
        assert "NOT DONE: " in out and "openspec change gate has unticked tasks" in out, out
    else:
        assert "openspec change gate was archived without DoD evidence" in out, out


def test_unredacted_secret_in_raw_output_fails(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    secret = "api_key=" + "x" * 24
    evidence["parent_confirmation"]["raw_output"] = _artifact(
        root, "confirm.txt", f"drill: orphan module FAILED as intended\n{secret}\n"
    )
    assert any("unredacted credential" in e for e in _check(root, evidence))


def test_verdicts_own_missing_credential_note_is_not_a_credential(
    repo: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate log that holds Verdict's credential_env_missing note holds no secret."""
    from verdict.provider_bootstrap import resolve_provider_bootstrap

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    root, evidence = repo
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"},
        config_path=tmp_path / "absent.yaml",
        credential_store_env={},
    )
    notes = "".join(f"verdict: {note.describe()}\n" for note in result.notes())
    assert "credential_env_missing" in notes
    evidence["parent_confirmation"]["raw_output"] = _artifact(
        root, "confirm.txt", f"{notes}drill: orphan module FAILED as intended\n"
    )
    assert _check(root, evidence) == []


def test_repo_tooling_story_must_not_change_product_code(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    (root / "verdict").mkdir()
    (root / "verdict/x.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "x")
    sha = _git(root, "rev-parse", "HEAD")
    evidence = _mutate(evidence, "merged_sha", sha)
    errors = _check(root, evidence)
    assert any("changed product code: verdict/x.py" in e for e in errors), errors


def test_all_flags_an_archive_without_evidence(
    repo: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, evidence = repo
    (root / "openspec/changes/archive/2026-10-14-unproven").mkdir(parents=True)
    (root / "openspec/changes/archive/2026-10-01-backdated").mkdir(parents=True)
    (root / "openspec/changes/archive/2026-10-09-bod-198-codiv-candidate").mkdir(parents=True)
    (root / "docs/proof/dod/gate.json").write_text(json.dumps(evidence), encoding="utf-8")
    assert check_dod.main(["--all", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "DONE: " in out
    assert "openspec change unproven was archived without DoD evidence" in out
    assert "openspec change backdated was archived without DoD evidence" in out
    assert "bod-198-codiv-candidate" not in out


def test_repository_evidence_passes() -> None:
    assert check_dod.main(["--all"]) == 0


def test_root_without_a_schema_copy_is_checked_with_the_checker_schema(
    repo: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, evidence = repo
    assert not (root / "proof").exists()
    (root / "docs/proof/dod/gate.json").write_text(json.dumps(evidence), encoding="utf-8")
    assert check_dod.main(["--all", "--root", str(root)]) == 0
    assert "DONE: " in capsys.readouterr().out


NO_CHANGE = {"status": "not_applicable", "reason": "dead code removal, no behaviour change"}


def _no_change_story(
    root: Path, evidence: dict[str, Any], story: str = "cleanup", extra: str | None = None
) -> dict[str, Any]:
    """Evidence for ``story``, merged in a new commit, that names no OpenSpec change."""
    (root / "scripts/tool.py").write_text(f"print({story!r})\n", encoding="utf-8")
    if extra is not None:
        path = root / extra
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# touched\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", story)
    sha = _git(root, "rev-parse", "HEAD")
    changed = copy.deepcopy(evidence)
    changed.update(story=story, merged_sha=sha, openspec=dict(NO_CHANGE))
    changed["on_main"]["sha"] = sha
    for item in changed["acceptance"]:
        item["first_run"]["sha"] = item["fresh_run"]["sha"] = sha
    return changed


def _check_story(root: Path, evidence: dict[str, Any]) -> list[str]:
    path = root / f"docs/proof/dod/{evidence['story']}.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return check_dod.check_evidence(root, path)


def test_story_with_no_openspec_change_is_done(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    assert _check_story(root, _no_change_story(root, evidence)) == []


@pytest.mark.parametrize(
    "touched", ["openspec/changes/other/tasks.md", "openspec/specs/other/spec.md"]
)
def test_no_change_claim_fails_when_the_merged_commit_changed_openspec(
    repo: tuple[Path, dict[str, Any]], touched: str
) -> None:
    root, evidence = repo
    errors = _check_story(root, _no_change_story(root, evidence, extra=touched))
    assert any("merged commit changed" in e and touched in e for e in errors), errors


@pytest.mark.parametrize(
    "change",
    [
        "openspec/changes/bod-7",
        "openspec/changes/bod-7-thing",
        "openspec/changes/bod7-thing",
        "openspec/changes/BOD-7-thing",
        "openspec/changes/archive/2026-10-20-bod-7-thing",
    ],
)
def test_no_change_claim_fails_when_a_change_is_named_after_the_story(
    repo: tuple[Path, dict[str, Any]], change: str
) -> None:
    root, evidence = repo
    story = _no_change_story(root, evidence, story="bod-7-cleanup")
    (root / change).mkdir(parents=True)
    errors = _check_story(root, story)
    assert any("named after this story" in e for e in errors), errors


def test_a_change_for_another_ticket_does_not_block_a_no_change_story(
    repo: tuple[Path, dict[str, Any]],
) -> None:
    root, evidence = repo
    story = _no_change_story(root, evidence, story="bod-7")
    (root / "openspec/changes/bod-70-thing").mkdir(parents=True)
    (root / "openspec/changes/bod70-thing").mkdir(parents=True)
    assert _check_story(root, story) == []


def test_a_story_without_a_ticket_is_matched_by_its_whole_name(
    repo: tuple[Path, dict[str, Any]],
) -> None:
    root, evidence = repo
    story = _no_change_story(root, evidence)
    (root / "openspec/changes/cleanup-later").mkdir(parents=True)
    errors = _check_story(root, story)
    assert any("named after this story" in e for e in errors), errors
    shutil.move(root / "openspec/changes/cleanup-later", root / "openspec/changes/cleanups")
    assert _check_story(root, story) == []


@pytest.mark.parametrize(
    "openspec",
    [
        {},
        {"status": "not_applicable"},
        {"status": "not_applicable", "reason": "short"},
        {"status": "skipped", "reason": NO_CHANGE["reason"]},
        {"change": "gate", **NO_CHANGE},
        {**NO_CHANGE, "extra": "x"},
    ],
)
def test_no_change_needs_the_exact_shape_and_a_reason(
    repo: tuple[Path, dict[str, Any]], openspec: dict[str, str]
) -> None:
    root, evidence = repo
    story = _no_change_story(root, evidence)
    story["openspec"] = openspec
    errors = _check_story(root, story)
    assert any(e.startswith("openspec") for e in errors), errors


def test_both_no_change_guards_report_together(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    story = _no_change_story(
        root, evidence, story="bod-7-cleanup", extra="openspec/changes/bod-7-thing/tasks.md"
    )
    errors = _check_story(root, story)
    assert any("merged commit changed" in e for e in errors), errors
    assert any("named after this story" in e for e in errors), errors


def test_the_other_checks_still_run_for_a_no_change_story(
    repo: tuple[Path, dict[str, Any]],
) -> None:
    root, evidence = repo
    story = _no_change_story(root, evidence, extra="verdict/thing.py")
    assert any("changed product code" in e for e in _check_story(root, story))
    story["parent_confirmation"]["by"] = story["author"]
    assert any("cannot confirm their own story" in e for e in _check_story(root, story))


def test_all_still_flags_an_unproven_archive_beside_a_no_change_story(
    repo: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, evidence = repo
    story = _no_change_story(root, evidence)
    (root / "docs/proof/dod/cleanup.json").write_text(json.dumps(story), encoding="utf-8")
    assert check_dod.main(["--all", "--root", str(root)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("DONE: ") and "cleanup.json" in line for line in lines), lines
    assert "NOT DONE: openspec change gate was archived without DoD evidence" in lines, lines


def _structured_result(
    *,
    outcome="COMPLETE",
    review_status="PASS",
    run_id="run-1",
    started_at="2099-01-01T00:00:00Z",
    finished_at="2099-01-01T00:10:00Z",
):
    return json.dumps(
        {
            "outcome": outcome,
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "events_digest": "a" * 64,
            "review": {"status": review_status},
            "marker": RUN_URL,
            "drill": "orphan module FAILED as intended",
        }
    )


def _structured_evidence(root, evidence, *, review_status="PASS"):
    item = evidence["acceptance"][0]
    for index, (run, name) in enumerate(
        ((item["first_run"], "structured-first.json"), (item["fresh_run"], "structured-fresh.json"))
    ):
        run["raw_output"] = _artifact(
            root,
            name,
            _structured_result(
                review_status=review_status,
                run_id=f"run-{index}",
                started_at="2099-01-02T00:00:00Z" if index else "2099-01-01T00:00:00Z",
                finished_at="2099-01-02T00:10:00Z" if index else "2099-01-01T00:10:00Z",
            ),
        )
        run["at"] = "2099-01-01T00:05:00Z" if index == 0 else "2099-01-02T00:05:00Z"
    evidence["parent_confirmation"]["raw_output"] = _artifact(
        root, "structured-confirm.json", _structured_result(run_id="run-confirm")
    )
    evidence["parent_confirmation"]["at"] = "2099-01-01T00:06:00Z"


def test_structured_result_exit_and_outcome_must_agree(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    evidence["acceptance"][0]["first_run"]["exit_code"] = 1
    evidence["acceptance"][0]["expect_exit_code"] = 1
    evidence["acceptance"][0]["fresh_run"]["exit_code"] = 1
    errors = _check(root, evidence)
    assert any("exit_code 1" in e and "outcome COMPLETE" in e for e in errors), errors


def test_structured_result_exit_and_outcome_agreement_passes(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    assert _check(root, evidence) == []


def test_structured_result_time_must_be_in_own_window(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    evidence["acceptance"][0]["first_run"]["at"] = "2099-01-01T00:20:00Z"
    errors = _check(root, evidence)
    assert any("at" in e and "started_at" in e and "finished_at" in e for e in errors), errors


def test_structured_result_time_in_own_window_passes(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    assert _check(root, evidence) == []


def test_structured_result_review_must_pass_for_zero_exit(repo):
    root, evidence = repo
    _structured_evidence(root, evidence, review_status="ERROR")
    errors = _check(root, evidence)
    assert any("review status ERROR" in e and "PASS" in e for e in errors), errors


def test_structured_result_review_passes_for_zero_exit(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    assert _check(root, evidence) == []


def test_same_raw_output_cannot_serve_as_two_runs(repo):
    root, evidence = repo
    first = evidence["acceptance"][0]["first_run"]["raw_output"]
    evidence["acceptance"][0]["fresh_run"]["raw_output"] = first
    errors = _check(root, evidence)
    assert "the same raw output cannot serve as two runs" in errors, errors


def test_distinct_raw_outputs_are_valid(repo):
    root, evidence = repo
    assert _check(root, evidence) == []


def test_non_structured_raw_output_is_unchanged(repo):
    root, evidence = repo
    assert _check(root, evidence) == []


ORCH_COMMAND = "verdict orchestrate --graph graph.json --repo /tmp/x --runs-dir /tmp/y --executor prime-headless --json"


def _orchestrate_evidence(root, evidence, output):
    for index, run in enumerate(
        (evidence["acceptance"][0]["first_run"], evidence["acceptance"][0]["fresh_run"])
    ):
        run["command"] = ORCH_COMMAND
        run["raw_output"] = _artifact(root, f"orchestrate-{index}.txt", output)
    confirmation = evidence["parent_confirmation"]
    confirmation["command"] = ORCH_COMMAND
    confirmation["raw_output"] = _artifact(root, "orchestrate-confirm.txt", output)


def test_orchestrate_command_rejects_hand_built_summary(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, '{"story": "x", "integrate": {}}')
    errors = _check(root, evidence)
    assert all("raw output is not the run" in e for e in errors if "orchestrate" in e), errors
    assert len([e for e in errors if "raw output is not the run" in e]) == 3


def test_orchestrate_command_rejects_empty_output(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, "")
    errors = _check(root, evidence)
    assert len([e for e in errors if "raw output is not the run" in e]) == 3


def test_orchestrate_command_rejects_leading_noise(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, "stderr warning\n" + _structured_result())
    errors = _check(root, evidence)
    assert len([e for e in errors if "stdout only" in e]) == 3


def test_orchestrate_command_accepts_result_json(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, _structured_result() + "\n")
    evidence["freshness_window_hours"] = 0.01
    evidence["acceptance"][0]["fresh_run"]["at"] = "2099-01-01T00:06:00Z"
    evidence["acceptance"][0]["fresh_run"]["raw_output"] = _artifact(
        root, "structured-fresh-2.txt", _structured_result(run_id="fresh-2") + "\n\n"
    )
    evidence["parent_confirmation"]["raw_output"] = _artifact(
        root, "structured-confirm-2.txt", _structured_result(run_id="confirm-2") + "\n"
    )
    evidence["parent_confirmation"]["at"] = "2099-01-01T00:06:00Z"
    assert _check(root, evidence) == []


def test_non_orchestrate_json_is_unaffected(repo):
    root, evidence = repo
    output = (
        '{"story": "x", "integrate": {}}\n'
        + RUN_URL
        + "\ndrill: orphan module FAILED as intended\n"
    )
    _orchestrate_evidence(root, evidence, output)
    for run in (evidence["acceptance"][0]["first_run"], evidence["acceptance"][0]["fresh_run"]):
        run["command"] = "verdict route --json"
    evidence["parent_confirmation"]["command"] = "verdict route --json"
    evidence["acceptance"][0]["first_run"]["raw_output"] = _artifact(
        root, "route-first.txt", output + " first"
    )
    evidence["acceptance"][0]["fresh_run"]["raw_output"] = _artifact(
        root, "route-fresh.txt", output + " fresh"
    )
    evidence["parent_confirmation"]["raw_output"] = _artifact(root, "route-confirm.txt", output)
    assert _check(root, evidence) == []


def test_blocked_structured_parent_confirmation_fails(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, _structured_result() + "\n")
    blocked = _structured_result(outcome="BLOCKED", review_status="ERROR", run_id="blocked")
    evidence["parent_confirmation"]["raw_output"] = _artifact(root, "blocked-confirm.txt", blocked)
    errors = _check(root, evidence)
    assert any("outcome BLOCKED" in e and "COMPLETE" in e for e in errors), errors


def test_structured_parent_confirmation_time_must_be_in_window(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, _structured_result() + "\n")
    evidence["parent_confirmation"]["at"] = "2099-01-01T00:20:00Z"
    errors = _check(root, evidence)
    assert any("parent_confirmation" in e and "outside result window" in e for e in errors), errors


def test_orchestrate_command_matcher(repo):
    assert check_dod._is_orchestrate_json_command(ORCH_COMMAND)
    assert check_dod._is_orchestrate_json_command("/path/to/.venv/bin/verdict orchestrate --json")
    assert check_dod._is_orchestrate_json_command("python -m verdict orchestrate --json")
    assert check_dod._is_orchestrate_json_command("env X=1 verdict orchestrate --json")
    assert check_dod._is_orchestrate_json_command("timeout 900 verdict orchestrate --json")
    assert not check_dod._is_orchestrate_json_command("verdict route --json")
    assert not check_dod._is_orchestrate_json_command("verdict orchestrate")


def test_orchestrate_regression_key_shapes_fail_closed(repo):
    root, evidence = repo
    shapes = [
        '{"story":"x","merged_sha":"x","installed_main_sha":"x","run_dir":"x","command":"x","note":"x","worker_terminals":[],"selections":[],"node_states":[],"integrate":{}}',
        '{"run_id":"x","events":[]}',
        '{"outcome":"BLOCKED","reason":"review status ERROR"}',
    ]
    for index, (run, output) in enumerate(
        zip(
            (
                evidence["acceptance"][0]["first_run"],
                evidence["acceptance"][0]["fresh_run"],
                evidence["parent_confirmation"],
            ),
            shapes,
            strict=True,
        )
    ):
        run["command"] = ORCH_COMMAND
        run["raw_output"] = _artifact(root, f"hand-built-{index}.json", output)
    errors = _check(root, evidence)
    assert len([e for e in errors if "raw output is not the run" in e]) == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", "2026-11-16T18:10:41"),
        ("finished_at", "2026-11-16T18:10:41"),
        ("started_at", 12345),
        ("finished_at", None),
    ],
)
def test_structured_result_invalid_time_fails_without_crashing(repo, field, value):
    root, evidence = repo
    _structured_evidence(root, evidence)
    result = json.loads(_structured_result())
    result[field] = value
    evidence["acceptance"][0]["first_run"]["raw_output"] = _artifact(
        root, "invalid-time.json", json.dumps(result)
    )
    errors = _check(root, evidence)
    assert any("structured result time is invalid" in e for e in errors), errors


def test_structured_result_timezone_offsets_and_fraction_pass(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    result = _structured_result(
        started_at="2026-11-16T18:10:41.123+00:00", finished_at="2026-11-16T18:10:41.456+00:00"
    )
    evidence["acceptance"][0]["first_run"]["raw_output"] = _artifact(
        root, "offset-time.json", result
    )
    evidence["acceptance"][0]["first_run"]["at"] = "2026-11-16T18:10:41.200+00:00"
    assert not any("structured result time is invalid" in e for e in _check(root, evidence))


def test_structured_result_inverted_interval_fails(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    result = _structured_result(
        started_at="2026-11-16T18:14:00Z", finished_at="2026-11-16T18:10:00Z"
    )
    evidence["acceptance"][0]["first_run"]["raw_output"] = _artifact(root, "inverted.json", result)
    errors = _check(root, evidence)
    assert any("started_at" in e and "finished_at" in e and "later" in e for e in errors), errors


def test_structured_parent_confirmation_review_must_pass(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, _structured_result() + "\n")
    evidence["parent_confirmation"]["raw_output"] = _artifact(
        root, "error-review.json", _structured_result(review_status="ERROR")
    )
    errors = _check(root, evidence)
    assert any("parent_confirmation" in e and "review status ERROR" in e for e in errors), errors


def test_structured_parent_confirmation_complete_passes(repo):
    root, evidence = repo
    _orchestrate_evidence(root, evidence, _structured_result() + "\n")
    assert not any(
        "parent_confirmation" in e and "review status" in e for e in _check(root, evidence)
    )


@pytest.mark.parametrize(
    "command,expected",
    [
        ("verdict orchestrate -j", False),
        ("verdict orchestrate --json=true", False),
        ("v orchestrate --json", False),
        ("verdict  orchestrate --json", True),
        ("verdict orchestrate --graph 'my graph.json' --json", True),
    ],
)
def test_orchestrate_command_matcher_boundaries(command, expected):
    assert check_dod._is_orchestrate_json_command(command) is expected


def test_same_run_id_cannot_serve_as_two_structured_runs(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    first = evidence["acceptance"][0]["first_run"]["raw_output"]
    data = (root / first["path"]).read_text() + "\n"
    evidence["acceptance"][0]["fresh_run"]["raw_output"] = _artifact(root, "same-run-id.json", data)
    errors = _check(root, evidence)
    assert "the same run_id cannot serve as two runs" in errors, errors


def test_distinct_structured_run_ids_pass(repo):
    root, evidence = repo
    _structured_evidence(root, evidence)
    assert "the same run_id cannot serve as two runs" not in _check(root, evidence)
