#!/usr/bin/env python3
"""Executable definition of done (AGENTS.md "Definition of done").

A story is DONE only when its evidence file ``docs/proof/dod/<story>.json``
passes this check. The file follows ``proof/dod.schema.json`` and points at
raw-output artifacts under ``docs/proof/dod/`` by path and SHA-256.

The check fails on any missing or mismatched field:

* every run SHA is the merged SHA or a later commit, and is an ancestor of HEAD;
* each raw output exists, matches its digest, contains the expected text and the
  real-state marker, and holds nothing that ``redact_text`` would remove;
* the fresh run is at least ``freshness_window_hours`` after the first run, with
  the same command;
* ``product`` stories need a green setup/doctor result and operator-host runs;
* ``repo-tooling`` stories must not change ``verdict/`` in the merged commit;
* the OpenSpec change is archived and every task in it is ticked, including
  ``- [ ] (post-merge)`` tasks, which ``scripts/check_openspec_tasks.py`` lets
  stay open at merge time;
* a story that changed no behaviour may name no change: ``"openspec":
  {"status": "not_applicable", "reason": "..."}``. That fails when the merged
  commit changed anything under ``openspec/`` or when a change, active or
  archived, is named after the story's ticket;
* the parent confirmation is by someone other than the author, re-runs one of
  the acceptance commands and shows the expected output.

``--all`` checks every evidence file, and fails when an OpenSpec change was
archived after this gate (not in ``PRE_GATE_ARCHIVES``) without an evidence
file that names it.

Usage:
    scripts/check_dod.py --all
    scripts/check_dod.py docs/proof/dod/<story>.json [...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from verdict.security import redact_text  # noqa: E402

# The schema belongs to this checker, so it is read from the checker's own repository.
SCHEMA_PATH = Path("proof/dod.schema.json")
EVIDENCE_DIR = Path("docs/proof/dod")
ARCHIVE_DIR = Path("openspec/changes/archive")
# Changes archived before this gate existed are not re-judged. Every later
# archive needs an evidence file, whatever date its directory name carries.
PRE_GATE_ARCHIVES = frozenset(
    {"2026-10-08-bod-288-review-coverage-fail-closed", "2026-10-09-bod-198-codiv-candidate"}
)
ACTIONS_RUN_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+")
# The ticket at the start of a story name: "bod-384" in "bod-384-dead-code".
TICKET = re.compile(r"([a-z]+)-(\d+)")
ARCHIVE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}-")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{value!r} must be an ISO-8601 time with a timezone")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} must be an ISO-8601 time with a timezone")
    return parsed


STRUCTURED_KEYS = ("outcome", "run_id", "started_at", "finished_at", "events_digest")
# json.loads raises JSONDecodeError (a ValueError), plain ValueError for an integer
# over the digit limit, and RecursionError for deep nesting.
NOT_JSON = (ValueError, RecursionError)
RESULT_TIME_TOLERANCE = timedelta(minutes=5)


def _command_tokens(command: str) -> list[str]:
    """Shell words of a recorded command, with ``;``, ``>``, ``(`` and the like split off."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        # Recorded commands need not be valid shell (a stray apostrophe in a goal).
        # A parse error must add the structured-stdout requirement, never remove it.
        return [token.strip("'\"") for token in re.split(r"[\s()<>|&;]+", command)]


def _is_orchestrate_json_command(command: str) -> bool:
    tokens = _command_tokens(command)
    starts = [
        index
        for index, token in enumerate(tokens)
        if Path(token).name == "verdict"
        or (token == "verdict.cli" and index > 0 and tokens[index - 1] == "-m")
    ]
    if not starts or "orchestrate" not in tokens[starts[0] + 1 :]:
        return False
    # argparse accepts any unambiguous prefix of a long option: --j, --js and --jso set json too.
    return any(len(token) >= 3 and "--json".startswith(token) for token in tokens)


def _structured_result(text: str) -> dict[str, Any] | None:
    try:
        result = json.loads(text)
    except NOT_JSON:
        return None
    if not isinstance(result, dict):
        return None
    required = set(STRUCTURED_KEYS)
    return result if required <= result.keys() else None


def _requires_result_json(command: str, want: int) -> bool:
    """Whether a run of ``command`` that should exit ``want`` must record result JSON.

    ``verdict orchestrate --json`` prints its result when it exits 0 or 1. Any other
    exit code is a startup block: one line on stderr and nothing on stdout.
    """
    return want in (0, 1) and _is_orchestrate_json_command(command)


def _check_run_truth(
    run: dict[str, Any], text: str, where: str, errors: list[str], *, want: int = 0
) -> None:
    """Check a run against its own structured result.

    ``want`` is the exit code its acceptance item expects. An acceptance run records
    its exit code; a parent confirmation records none and is judged by ``want``.
    """
    required = _requires_result_json(run["command"], want)
    result = _structured_result(text.rstrip()) if text == text.lstrip() else None
    if result is None:
        if required:
            try:
                parsed = json.loads(text)
                missing = [
                    key
                    for key in STRUCTURED_KEYS
                    if not isinstance(parsed, dict) or key not in parsed
                ]
            except NOT_JSON:
                missing = list(STRUCTURED_KEYS)
            errors.append(
                f"{where}: command is {run['command']!r} but the raw output is not the run's result JSON "
                f"(missing: {', '.join(missing) or 'stdout only'}; stdout only)"
            )
        return
    run_id = result["run_id"]
    if not isinstance(run_id, str) or not run_id:
        errors.append(f"{where}: run_id {run_id!r} is not a non-empty string")
    outcome = result["outcome"]
    exit_code = run.get("exit_code")
    complete = outcome == "COMPLETE"
    if exit_code is not None and (exit_code == 0) != complete:
        errors.append(f"{where}: exit_code {exit_code} disagrees with outcome {outcome}")
    try:
        started = _time(result["started_at"])
        finished = _time(result["finished_at"])
        recorded = _time(run["at"])
        # A time at the edge of the calendar overflows here, not in _time.
        earliest = started - RESULT_TIME_TOLERANCE
        latest = finished + RESULT_TIME_TOLERANCE
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        errors.append(f"{where}: structured result time is invalid: {exc}")
    else:
        if started > finished:
            errors.append(
                f"{where}: started_at {result['started_at']!r} is later than finished_at {result['finished_at']!r}"
            )
        if not (earliest <= recorded <= latest):
            errors.append(
                f"{where}: at {run['at']} is outside result window "
                f"started_at {result['started_at']} to finished_at {result['finished_at']} (5 minute tolerance)"
            )
    if "exit_code" not in run:
        if want != 0:
            # A story that proves a blocked run: the confirmation must be blocked too.
            if complete:
                errors.append(
                    f"{where}: outcome {outcome}, but the acceptance item expects exit code {want} "
                    "(an outcome other than COMPLETE required)"
                )
            return
        if not complete:
            errors.append(f"{where}: outcome {outcome} is not COMPLETE (COMPLETE required)")
        review = result.get("review")
        status = review.get("status") if isinstance(review, dict) else None
        if status != "PASS":
            errors.append(f"{where}: review status {status} is not PASS (PASS required)")
        return
    if exit_code == 0:
        review = result.get("review")
        status = review.get("status") if isinstance(review, dict) else None
        if status != "PASS":
            errors.append(f"{where}: review status {status} is not PASS (PASS required)")


def _artifact(root: Path, artifact: dict[str, str], where: str, errors: list[str]) -> str | None:
    path = (root / artifact["path"]).resolve()
    if not path.is_relative_to((root / EVIDENCE_DIR).resolve()):
        errors.append(f"{where}: raw output {artifact['path']} is outside {EVIDENCE_DIR}")
        return None
    try:
        data = path.read_bytes()
    except OSError:
        errors.append(f"{where}: raw output {artifact['path']} is missing")
        return None
    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        errors.append(f"{where}: raw output {artifact['path']} does not match its sha256")
    text = data.decode("utf-8", "replace")
    if redact_text(text) != text:
        errors.append(f"{where}: raw output {artifact['path']} holds an unredacted credential")
    return text


def _run(
    root: Path, evidence: dict[str, Any], run: dict[str, Any], where: str, errors: list[str]
) -> str | None:
    merged = evidence["merged_sha"]
    if not _is_ancestor(root, merged, run["sha"]):
        errors.append(f"{where}: sha {run['sha'][:12]} is not the merged sha or a later commit")
    if not _is_ancestor(root, run["sha"], "HEAD"):
        errors.append(f"{where}: sha {run['sha'][:12]} is not on this branch")
    if evidence["kind"] == "product" and run["environment"] != "operator-host":
        errors.append(f"{where}: a product story must run on the operator host")
    if run["environment"] == "github-actions" and not ACTIONS_RUN_URL.search(
        run["real_state_marker"]
    ):
        errors.append(f"{where}: a github-actions run needs its run URL as the real-state marker")
    text = _artifact(root, run["raw_output"], where, errors)
    if text is not None and run["real_state_marker"] not in text:
        errors.append(f"{where}: real-state marker is not in the raw output")
    return text


def _openspec(root: Path, change: str, errors: list[str]) -> None:
    if (root / "openspec/changes" / change).exists():
        errors.append(f"openspec change {change} is not archived")
        return
    archived = sorted((root / ARCHIVE_DIR).glob(f"*-{change}"))
    if not archived:
        errors.append(f"openspec change {change} is not in {ARCHIVE_DIR}")
        return
    tasks = archived[-1] / "tasks.md"
    if not tasks.is_file():
        errors.append(f"openspec change {change} has no tasks.md")
    elif re.search(r"^\s*- \[ \]", tasks.read_text(encoding="utf-8"), re.M):
        errors.append(f"openspec change {change} has unticked tasks")


def _merged_files(root: Path, merged: str) -> list[str]:
    """Paths the merged commit changed against its first parent."""
    return _git(
        root, "diff-tree", "-m", "--first-parent", "--no-commit-id", "--name-only", "-r", merged
    ).stdout.splitlines()


def _no_openspec_change(root: Path, story: str, merged: str, errors: list[str]) -> None:
    """Reasons the claim "this story has no OpenSpec change" is false."""
    touched = [name for name in _merged_files(root, merged) if name.startswith("openspec/")]
    if touched:
        errors.append(
            f"openspec: not_applicable, but the merged commit changed {', '.join(touched[:5])}"
        )
    ticket = TICKET.match(story)
    # Changes are named "bod-384-..." or "bod384-..."; "bod-3840-..." is another ticket.
    # Story names are lower case (schema); a change directory may not be.
    owned = re.compile(
        rf"{re.escape(ticket.group(1))}-?{ticket.group(2)}(?!\d)"
        if ticket
        else rf"{re.escape(story)}(-|$)",
        re.IGNORECASE,
    )
    changes = root / "openspec/changes"
    found = [p for p in (*changes.glob("*"), *(root / ARCHIVE_DIR).glob("*")) if p.is_dir()]
    for directory in sorted(found):
        name = directory.name
        if directory.parent.name == "archive":
            name = ARCHIVE_DATE.sub("", name, count=1)
        elif name == "archive":
            continue
        if owned.match(name):
            errors.append(f"openspec: not_applicable, but change {name} is named after this story")


def check_evidence(root: Path, path: Path) -> list[str]:
    """Return every reason the evidence file at ``path`` does not prove DONE."""
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, *NOT_JSON) as exc:
        return [f"cannot read {path}: {exc}"]
    schema = json.loads((ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(evidence), key=lambda e: list(e.path)
    )
    if schema_errors:
        return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in schema_errors]

    errors: list[str] = []
    if evidence["story"] != path.stem:
        errors.append(f"story {evidence['story']!r} does not match the file name {path.name}")
    merged = evidence["merged_sha"]
    if _git(root, "cat-file", "-e", f"{merged}^{{commit}}").returncode != 0:
        return [*errors, f"merged_sha {merged[:12]} is not a commit in this repository"]
    if not _is_ancestor(root, merged, "HEAD"):
        errors.append(f"merged_sha {merged[:12]} is not on this branch")
    merged_at = _time(_git(root, "show", "-s", "--format=%cI", merged).stdout.strip())

    if evidence["kind"] == "repo-tooling":
        product = [name for name in _merged_files(root, merged) if name.startswith("verdict/")]
        if product:
            errors.append(f"repo-tooling story changed product code: {', '.join(product[:5])}")
    elif evidence["setup_doctor"]["status"] != "green":
        errors.append("a product story needs a green setup/doctor result")
    if evidence["setup_doctor"]["status"] == "green":
        _artifact(root, evidence["setup_doctor"]["raw_output"], "setup_doctor", errors)

    on_main = evidence["on_main"]
    _run(root, evidence, on_main, "on_main", errors)
    if on_main["sha"] != merged:
        errors.append("on_main: sha must be the merged sha")
    if on_main["exit_code"] != 0:
        errors.append(f"on_main: exit code {on_main['exit_code']}, required checks not green")

    window = timedelta(hours=evidence["freshness_window_hours"])
    expected: dict[str, str] = {}
    wanted: dict[str, int] = {}
    for index, item in enumerate(evidence["acceptance"]):
        want = item.get("expect_exit_code", 0)
        first, fresh = item["first_run"], item["fresh_run"]
        expected[first["command"]] = item["expect"]
        wanted[first["command"]] = want
        for label, run in (("first_run", first), ("fresh_run", fresh)):
            where = f"acceptance[{index}].{label}"
            text = _run(root, evidence, run, where, errors)
            if text is not None and item["expect"] not in text:
                errors.append(f"{where}: expected text is not in the raw output")
            if run["exit_code"] != want:
                errors.append(f"{where}: exit code {run['exit_code']}, expected {want}")
            if text is not None:
                _check_run_truth(run, text, where, errors, want=want)
        if _time(first["at"]) < merged_at:
            errors.append(f"acceptance[{index}].first_run: ran before the merge")
        if fresh["command"] != first["command"]:
            errors.append(f"acceptance[{index}].fresh_run: command differs from the first run")
        if _time(fresh["at"]) - _time(first["at"]) < window:
            errors.append(f"acceptance[{index}].fresh_run: not after the freshness window")

    if "change" in evidence["openspec"]:
        _openspec(root, evidence["openspec"]["change"], errors)
    else:
        _no_openspec_change(root, evidence["story"], merged, errors)

    run_artifacts = [
        (f"acceptance[{index}].first_run", item["first_run"])
        for index, item in enumerate(evidence["acceptance"])
    ] + [
        (f"acceptance[{index}].fresh_run", item["fresh_run"])
        for index, item in enumerate(evidence["acceptance"])
    ]
    confirmation = evidence["parent_confirmation"]
    run_artifacts.append(("parent_confirmation", confirmation))
    seen: set[str] = set()
    run_ids: set[str] = set()
    for _where, run in run_artifacts:
        digest = run["raw_output"]["sha256"]
        if digest in seen:
            errors.append("the same raw output cannot serve as two runs")
        seen.add(digest)
        text = _artifact(root, run["raw_output"], _where, errors)
        if text is not None:
            result = _structured_result(text.rstrip()) if text == text.lstrip() else None
            if result is not None:
                run_id = result["run_id"]
                if not isinstance(run_id, str) or not run_id:
                    continue  # reported by _check_run_truth
                if run_id in run_ids:
                    errors.append("the same run_id cannot serve as two runs")
                run_ids.add(run_id)
    if confirmation["by"] == evidence["author"]:
        errors.append("parent_confirmation: the author cannot confirm their own story")
    if confirmation["command"] not in expected:
        errors.append("parent_confirmation: command is not one of the acceptance commands")
    if _time(confirmation["at"]) < merged_at:
        errors.append("parent_confirmation: confirmed before the merge")
    text = _artifact(root, confirmation["raw_output"], "parent_confirmation", errors)
    if text is not None:
        # The confirmation re-runs one acceptance command and must end as that item expects.
        _check_run_truth(
            confirmation,
            text,
            "parent_confirmation",
            errors,
            want=wanted.get(confirmation["command"], 0),
        )
    if text is not None and expected.get(confirmation["command"], "\0") not in text:
        errors.append("parent_confirmation: expected text is not in the raw output")
    return errors


def unproven_archives(root: Path, evidence_files: list[Path]) -> list[str]:
    """Changes archived after this gate that no evidence file names."""
    named = set()
    for path in evidence_files:
        try:
            named.add(json.loads(path.read_text(encoding="utf-8"))["openspec"]["change"])
        except (OSError, *NOT_JSON, KeyError, TypeError):
            continue
    missing = []
    for archived in sorted((root / ARCHIVE_DIR).glob("????-??-??-*")):
        change = archived.name[11:]
        if archived.name not in PRE_GATE_ARCHIVES and change not in named:
            missing.append(change)
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("evidence", nargs="*", type=Path)
    parser.add_argument("--all", action="store_true", help="check every evidence file")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    files = list(args.evidence)
    if args.all:
        files += sorted((root / EVIDENCE_DIR).glob("*.json"))
    if not files and not args.all:
        parser.error("name an evidence file or pass --all")

    failed = False
    encoding = sys.stdout.encoding or "utf-8"
    for path in files:
        errors = check_evidence(root, path)
        failed |= bool(errors)
        print(f"{'NOT DONE' if errors else 'DONE'}: {path}")
        for error in errors:
            # An error can quote raw output, and JSON can decode to text stdout cannot encode.
            print(f"  - {error}".encode(encoding, "backslashreplace").decode(encoding))
    if args.all:
        for change in unproven_archives(root, files):
            failed = True
            print(f"NOT DONE: openspec change {change} was archived without DoD evidence")
        print(f"check_dod: {len(files)} evidence file(s) checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
