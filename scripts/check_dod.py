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
* the OpenSpec change is archived and every task in it is ticked;
* the parent confirmation is by someone other than the author, re-runs one of
  the acceptance commands and shows the expected output.

``--all`` checks every evidence file, and fails when an OpenSpec change was
archived on or after ``GATE_START`` without an evidence file that names it.

Usage:
    scripts/check_dod.py --all
    scripts/check_dod.py docs/proof/dod/<story>.json [...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
# Changes archived before the gate existed are not re-judged.
GATE_START = "2026-10-13"
ACTIONS_RUN_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _artifact(root: Path, artifact: dict[str, str], where: str, errors: list[str]) -> str | None:
    path = root / artifact["path"]
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


def check_evidence(root: Path, path: Path) -> list[str]:
    """Return every reason the evidence file at ``path`` does not prove DONE."""
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
        changed = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", merged).stdout
        product = [name for name in changed.split() if name.startswith("verdict/")]
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
    for index, item in enumerate(evidence["acceptance"]):
        want = item.get("expect_exit_code", 0)
        first, fresh = item["first_run"], item["fresh_run"]
        expected[first["command"]] = item["expect"]
        for label, run in (("first_run", first), ("fresh_run", fresh)):
            where = f"acceptance[{index}].{label}"
            text = _run(root, evidence, run, where, errors)
            if text is not None and item["expect"] not in text:
                errors.append(f"{where}: expected text is not in the raw output")
            if run["exit_code"] != want:
                errors.append(f"{where}: exit code {run['exit_code']}, expected {want}")
        if _time(first["at"]) < merged_at:
            errors.append(f"acceptance[{index}].first_run: ran before the merge")
        if fresh["command"] != first["command"]:
            errors.append(f"acceptance[{index}].fresh_run: command differs from the first run")
        if _time(fresh["at"]) - _time(first["at"]) < window:
            errors.append(f"acceptance[{index}].fresh_run: not after the freshness window")

    _openspec(root, evidence["openspec"]["change"], errors)

    confirmation = evidence["parent_confirmation"]
    if confirmation["by"] == evidence["author"]:
        errors.append("parent_confirmation: the author cannot confirm their own story")
    if confirmation["command"] not in expected:
        errors.append("parent_confirmation: command is not one of the acceptance commands")
    if _time(confirmation["at"]) < merged_at:
        errors.append("parent_confirmation: confirmed before the merge")
    text = _artifact(root, confirmation["raw_output"], "parent_confirmation", errors)
    if text is not None and expected.get(confirmation["command"], "\0") not in text:
        errors.append("parent_confirmation: expected text is not in the raw output")
    return errors


def unproven_archives(root: Path, evidence_files: list[Path]) -> list[str]:
    """Changes archived since GATE_START that no evidence file names."""
    named = set()
    for path in evidence_files:
        try:
            named.add(json.loads(path.read_text(encoding="utf-8"))["openspec"]["change"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    missing = []
    for archived in sorted((root / ARCHIVE_DIR).glob("????-??-??-*")):
        date, change = archived.name[:10], archived.name[11:]
        if date >= GATE_START and change not in named:
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
    for path in files:
        errors = check_evidence(root, path)
        failed |= bool(errors)
        print(f"{'NOT DONE' if errors else 'DONE'}: {path}")
        for error in errors:
            print(f"  - {error}")
    if args.all:
        for change in unproven_archives(root, files):
            failed = True
            print(f"NOT DONE: openspec change {change} was archived without DoD evidence")
        print(f"check_dod: {len(files)} evidence file(s) checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
