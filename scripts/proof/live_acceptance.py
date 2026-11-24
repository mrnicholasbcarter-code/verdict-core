#!/usr/bin/env python3
"""Live acceptance checks for the sharded full proof (issue #1307, design D9).

Each subcommand turns one live acceptance task into a check that can fail. It prints
the figures it compared (``PASS`` or ``FAIL`` per assertion) and exits 0 only when
every assertion holds, 1 when one fails, 2 on a usage or fetch failure. All GitHub
access goes through ``run_gh`` so that tests replace it with a fake.

    live_acceptance.py merged-run --run ID --parent-run ID --parent-rev SHA --merged-rev SHA
    live_acceptance.py timing-leg --run ID
    live_acceptance.py rounds --pre FILE --post FILE [--rounds 10]
    live_acceptance.py unsharded --report PATH

``merged-run`` derives the number of tests the merged change added from node ids
collected at the parent and merged revisions, in this environment, not from a source
diff: a parametrized test that gains five cases adds five.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.proof.aggregate import (  # noqa: E402
    CollectionError,
    InputError,
    load_json_object,
    read_junit,
    subprocess_collector,
)
from scripts.proof.contract import ContractError, ProofContract, load_contract  # noqa: E402
from scripts.proof.runner import plan_gates, selection_collect_args  # noqa: E402

RESEARCH_MEDIAN_SECONDS = 531
START_DELAY_RULE_SECONDS = 60
# Variables git sets for a hook; they would override ``git -C`` (see check_not_weakened.py).
GIT_LOCAL_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
GH_TIMEOUT_SECONDS = 180
GIT_TIMEOUT_SECONDS = 300
OUTCOME_KEYS = frozenset({"passed", "failed", "skipped", "xfailed", "xpassed", "error", "errors"})
_SUMMARY = re.compile(r"((?:\d+ [A-Za-z]+(?:, )?)+) in [\d.]+s")
_HEAD = re.compile(r"[0-9a-f]{40}")
_LEG_JOB = re.compile(r"proof-shard \((\d+)\)")


class FetchError(Exception):
    """GitHub, git or an input could not be read: a usage or fetch failure (exit 2)."""


# ---------------------------------------------------------------------------
# The GitHub layer (replaced in tests) and the collection layer
# ---------------------------------------------------------------------------


def run_gh(args: Sequence[str]) -> str:
    """Run ``gh`` and return stdout. The only place this module talks to GitHub."""
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=GH_TIMEOUT_SECONDS
        )
    except FileNotFoundError as exc:
        raise FetchError("the gh CLI is not installed") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(f"gh could not run: {exc}") from exc
    if done.returncode != 0:
        raise FetchError(f"gh {' '.join(args[:3])} failed: {done.stderr.strip()[-300:]}")
    return done.stdout


def gh_json(path: str) -> Any:
    try:
        return json.loads(run_gh(["api", path]))
    except ValueError as exc:
        raise FetchError(f"gh api {path}: not JSON") from exc


def download_artifact(run_id: str, name: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    run_gh(["run", "download", run_id, "-n", name, "-D", str(dest)])
    return dest


def collect_ids(args: Sequence[str], cwd: Path | None = None) -> list[str]:
    """Node ids the pytest arguments select, collected independently in ``cwd``."""
    try:
        return subprocess_collector(cwd or Path.cwd())(args)
    except CollectionError as exc:
        raise FetchError(str(exc)) from exc


@contextmanager
def checkout_revision(rev: str, repo: Path) -> Iterator[Path]:
    """A clean copy of a revision in a temporary directory, removed on every exit path.

    ``git archive`` reads the repository and changes nothing in it (no worktree entry,
    no index update).
    """
    root = Path(tempfile.mkdtemp(prefix="proof-revision-"))
    try:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar", rev],
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
                env={k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FetchError(f"git archive {rev} could not run: {exc}") from exc
        if done.returncode != 0:
            raise FetchError(
                f"git archive {rev} failed: {done.stderr.decode('utf-8', 'replace')[-300:]}"
            )
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(root, filter="data")
            else:  # pragma: no cover - older interpreters
                archive.extractall(root)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def resolve_revision(rev: str, repo: Path) -> str:
    """The full commit SHA a revision names in ``repo`` (read-only)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env={k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(f"git rev-parse {rev} could not run: {exc}") from exc
    sha = done.stdout.strip()
    if done.returncode != 0 or not _HEAD.fullmatch(sha):
        raise FetchError(f"{rev}: not a commit in {repo}")
    return sha


def collect_at_revision(rev: str, repo: Path, args: Sequence[str] | None = None) -> list[str]:
    """Node ids at one revision, collected in this environment against that revision's code."""
    wanted = list(args) if args is not None else ["tests/", "-q", "--tb=short"]
    with checkout_revision(rev, repo) as root:
        try:
            return subprocess_collector(root, import_from_root=True)(wanted)
        except CollectionError as exc:
            raise FetchError(f"collection at {rev} failed: {exc}") from exc


def diff_node_ids(parent: Sequence[str], merged: Sequence[str]) -> tuple[int, int]:
    """(added, removed): node ids only in the merged revision, and only in the parent."""
    before, after = set(parent), set(merged)
    return len(after - before), len(before - after)


def parse_pytest_summary(text: str) -> dict[str, int] | None:
    """The counts on the last pytest summary line of ``text``, or ``None`` when there is none."""
    for line in reversed(text.splitlines()):
        match = _SUMMARY.search(line)
        if match:
            return {word: int(n) for n, word in re.findall(r"(\d+) ([A-Za-z]+)", match.group(1))}
    return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class Checks:
    """Collects PASS and FAIL lines; ``code`` is 0 only when every assertion held."""

    def __init__(self) -> None:
        self.failed = False

    def note(self, line: str) -> None:
        print(line)

    def check(self, ok: bool, what: str, detail: str = "") -> None:
        self.failed = self.failed or not ok
        print(f"{'PASS' if ok else 'FAIL'} {what}" + (f": {detail}" if detail else ""))

    @property
    def code(self) -> int:
        return 1 if self.failed else 0


def _contract(path: Path) -> ProofContract:
    try:
        return load_contract(path)
    except ContractError as exc:
        raise FetchError(f"the proof contract could not be read: {exc}") from exc


def _shard_count(contract: ProofContract) -> int:
    if contract.shards is None:
        raise FetchError("the contract declares no modes.full.shards")
    return contract.shards


def _json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        return load_json_object(path, label)
    except InputError as exc:
        raise FetchError(str(exc)) from exc


def _gate_entries(report: dict[str, Any], label: str) -> list[dict[str, Any]]:
    gates = report.get("gates")
    if not isinstance(gates, list) or not all(isinstance(g, dict) for g in gates):
        raise FetchError(f"{label}: gates must be a list of objects")
    return gates


# ---------------------------------------------------------------------------
# merged-run
# ---------------------------------------------------------------------------


def _run_meta(run_id: str) -> dict[str, Any]:
    body = gh_json(f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}")
    if not isinstance(body, dict):
        raise FetchError(f"run {run_id}: no run metadata in the response")
    return body


def _bind_run(checks: Checks, label: str, run_id: str, expected_sha: str, main_branch: str) -> None:
    """The run really is the main push run of the revision it is said to be."""
    run = _run_meta(run_id)
    checks.check(
        run.get("head_sha") == expected_sha,
        f"{label} run {run_id} ran on the {label} revision",
        f"head_sha={run.get('head_sha')!r}, expected {expected_sha}",
    )
    checks.check(
        run.get("head_branch") == main_branch,
        f"{label} run {run_id} ran on the {main_branch} branch",
        f"head_branch={run.get('head_branch')!r}",
    )
    checks.check(
        run.get("event") == "push",
        f"{label} run {run_id} is a push run",
        f"event={run.get('event')!r}",
    )
    checks.check(
        run.get("name") == "Proof",
        f"{label} run {run_id} is the Proof workflow",
        f"name={run.get('name')!r}",
    )


def _check_merged_jobs(checks: Checks, run_id: str, count: int) -> None:
    """GitHub's own verdict on the legs and the aggregate, not the artifact's ``ok`` field."""
    jobs = _pages(f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs", "jobs")
    legs = sorted(
        (int(m.group(1)), j.get("conclusion"))
        for j in jobs
        if (m := _LEG_JOB.fullmatch(str(j.get("name"))))
    )
    checks.check(
        [index for index, _ in legs] == list(range(1, count + 1))
        and all(conclusion == "success" for _, conclusion in legs),
        f"merged run's {count} proof-shard legs all succeeded",
        f"found {legs}",
    )
    aggregates = [j for j in jobs if j.get("name") == "proof-full"]
    checks.check(
        len(aggregates) == 1 and aggregates[0].get("conclusion") == "success",
        "merged run's proof-full aggregate job succeeded",
        f"found {[j.get('conclusion') for j in aggregates]}",
    )


def cmd_merged_run(args: argparse.Namespace) -> int:
    contract = _contract(args.contract)
    count = _shard_count(contract)
    sharded = [g for g in contract.gates_for_mode("full") if g.shard is not None]
    if not any(g.shard == "split" for g in sharded):
        raise FetchError("the contract has shards but no full-mode gate with shard: split")
    checks = Checks()
    merged_sha = resolve_revision(args.merged_rev, args.root)
    parent_sha = resolve_revision(args.parent_rev, args.root)
    _bind_run(checks, "merged", args.run, merged_sha, args.main_branch)
    _bind_run(checks, "parent", args.parent_run, parent_sha, args.main_branch)
    _check_merged_jobs(checks, args.run, count)
    with tempfile.TemporaryDirectory(prefix="proof-live-") as tmp_name:
        tmp = Path(tmp_name)
        merged = _json_file(
            download_artifact(args.run, "proof-report-full", tmp / "merged")
            / "proof-report-full.json",
            "merged report",
        )
        shards = merged.get("shards")
        indexes = (
            [s.get("index") for s in shards if isinstance(s, dict)]
            if isinstance(shards, list)
            else []
        )
        checks.check(
            merged.get("ok") is True, "merged report ok is true", f"ok={merged.get('ok')!r}"
        )
        checks.check(
            indexes == list(range(1, count + 1)),
            f"merged report has one shards entry per declared leg (1..{count})",
            f"found {indexes}",
        )

        total_tests = total_skipped = 0
        for leg in range(1, count + 1):
            directory = download_artifact(args.run, f"proof-shard-{leg}", tmp / f"leg{leg}")
            for gate in sharded:
                junit = directory / f"junit-{gate.id}.xml"
                if not junit.is_file():
                    if gate.shard == "split" or leg == 1:
                        checks.check(False, f"leg {leg} has junit-{gate.id}.xml", "file missing")
                    continue
                try:
                    summary = read_junit(junit, f"leg {leg} junit-{gate.id}.xml")
                except InputError as exc:
                    checks.check(False, f"leg {leg} junit-{gate.id}.xml is readable", str(exc))
                    continue
                total_tests += summary.tests
                total_skipped += summary.skipped

        reference = [g for g in sharded if g.shard == "split"]
        collected = len(collect_ids(selection_collect_args(reference[0].command), args.root))
        parent_dir = download_artifact(args.parent_run, "proof-report-full", tmp / "parent")
        parent = _json_file(parent_dir / "proof-report-full.json", "parent report")
        unit_full = next(
            (g for g in _gate_entries(parent, "parent report") if g.get("id") == "unit_full"), None
        )
        counts = parse_pytest_summary(str(unit_full.get("log_slice", ""))) if unit_full else None
        if counts is None:
            raise FetchError("the parent run's unit_full gate has no pytest summary line to read")
        parent_total = sum(n for word, n in counts.items() if word in OUTCOME_KEYS)

    added, removed = diff_node_ids(
        collect_at_revision(parent_sha, args.root), collect_at_revision(merged_sha, args.root)
    )
    checks.note(f"summed JUnit tests: {total_tests}")
    checks.note(f"collected: {collected}")
    checks.note(f"parent total: {parent_total}")
    checks.note(f"added: {added}")
    checks.note(f"removed: {removed}")
    checks.note(f"summed JUnit skipped: {total_skipped}")
    checks.note(
        f"parent skipped: {counts.get('skipped', 0)}"
        f" parent xfailed: {counts.get('xfailed', 0)}"
        " (JUnit counts an expected failure as skipped: explain any difference)"
    )
    checks.check(
        total_tests == collected, "summed JUnit tests equal the independently collected count"
    )
    checks.check(
        total_tests == parent_total + added - removed,
        "summed JUnit tests equal the parent total plus added minus removed",
        f"{parent_total} + {added} - {removed} = {parent_total + added - removed}",
    )
    return checks.code


# ---------------------------------------------------------------------------
# timing-leg
# ---------------------------------------------------------------------------


def cmd_timing_leg(args: argparse.Namespace) -> int:
    contract = _contract(args.contract)
    leader = next((g for g in contract.gates_for_mode("full") if g.shard == "leader"), None)
    if leader is None:
        raise FetchError("the contract has no gate with shard: leader")
    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="proof-live-") as tmp_name:
        directory = download_artifact(args.run, "proof-shard-1", Path(tmp_name) / "leg1")
        report = _json_file(directory / "proof-report-full.json", "leg 1 report")
        status = next(
            (
                g.get("status")
                for g in _gate_entries(report, "leg 1 report")
                if g.get("id") == leader.id
            ),
            None,
        )
        checks.check(status == "pass", f"leg 1 {leader.id} passed", f"status={status!r}")
        junit_path = directory / f"junit-{leader.id}.xml"
        recorded: int | None = None
        if junit_path.is_file():
            try:
                recorded = read_junit(junit_path, f"junit-{leader.id}.xml").tests
            except InputError as exc:
                checks.check(False, f"junit-{leader.id}.xml is readable", str(exc))
        else:
            checks.check(False, f"leg 1 has junit-{leader.id}.xml", "file missing")
    collected = len(collect_ids(selection_collect_args(leader.command), args.root))
    checks.note(f"{leader.id} JUnit tests: {recorded}")
    checks.note(f"collected timing tests: {collected}")
    checks.check(collected > 0, "at least one timing test is collected")
    checks.check(
        recorded is not None and recorded == collected,
        f"{leader.id} JUnit tests equal the collected timing tests",
    )
    return checks.code


# ---------------------------------------------------------------------------
# rounds
# ---------------------------------------------------------------------------


def parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise FetchError(f"{label}: timestamp missing")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FetchError(f"{label}: not an ISO 8601 timestamp: {value!r}") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise FetchError(f"{label}: timestamp {value!r} has no UTC offset")
    return moment


@dataclass
class Leg:
    start_delay: float
    duration: float


@dataclass
class Round:
    head: str
    seconds: float
    proof_full_last: bool
    legs: list[Leg] = field(default_factory=list)


def _pages(path: str, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        body = gh_json(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        batch = body.get(key) if isinstance(body, dict) else None
        if not isinstance(batch, list):
            raise FetchError(f"gh api {path}: no {key!r} list in the response")
        items.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return items
        page += 1


def evaluate_head(head: str, *, post: bool, shards: int) -> Round | str:
    """The round a pull request head forms, or the reason it does not count."""
    runs = [
        r
        for r in _pages(f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={head}", "workflow_runs")
        if r.get("event") == "pull_request"
    ]
    if not runs:
        return "no pull_request runs found for the head"
    for run in runs:
        if run.get("status") != "completed":
            return f"run {run.get('name')} not finished (status {run.get('status')})"
        if run.get("conclusion") == "cancelled":
            return f"run {run.get('name')} was cancelled"
        if run.get("conclusion") == "action_required":
            return f"run {run.get('name')} is waiting for approval"
    created: list[datetime] = []
    named: list[dict[str, Any]] = []
    jobs: list[tuple[dict[str, Any], datetime, datetime | None, datetime | None]] = []
    for run in runs:
        run_created = parse_timestamp(run.get("created_at"), f"run {run.get('name')} created_at")
        created.append(run_created)
        for job in _pages(f"repos/{{owner}}/{{repo}}/actions/runs/{run['id']}/jobs", "jobs"):
            named.append(job)
            if job.get("status") == "completed" and job.get("conclusion") == "skipped":
                continue  # its condition skipped it: it never ran, so it has no execution time
            if job.get("status") != "completed" or job.get("completed_at") is None:
                return f"job {job.get('name')} of run {run.get('name')} not finished"
            if job.get("started_at") is None:
                return f"job {job.get('name')} of run {run.get('name')} has no start time"
            started = parse_timestamp(job.get("started_at"), f"job {job.get('name')} started_at")
            completed = parse_timestamp(
                job.get("completed_at"), f"job {job.get('name')} completed_at"
            )
            jobs.append((job, run_created, started, completed))
    if not jobs:
        return "the head's runs have no jobs"
    legs = [(j, rc, st, ct) for j, rc, st, ct in jobs if _LEG_JOB.fullmatch(str(j.get("name")))]
    if post:
        # Judged on every job, skipped ones included: a skipped leg is not a successful leg.
        named_legs = [j for j in named if _LEG_JOB.fullmatch(str(j.get("name")))]
        found = sorted(int(_LEG_JOB.fullmatch(str(j["name"])).group(1)) for j in named_legs)  # type: ignore[union-attr]
        if found != list(range(1, shards + 1)):
            return f"proof-shard legs found {found}, expected 1..{shards}"
        for job in named_legs:
            if job.get("conclusion") != "success":
                return f"{job['name']} did not succeed ({job.get('conclusion')})"
        aggregates = [j for j in named if j.get("name") == "proof-full"]
        if len(aggregates) != 1:
            return f"expected one proof-full job, found {len(aggregates)}"
        if aggregates[0].get("conclusion") != "success":
            return f"proof-full job did not succeed ({aggregates[0].get('conclusion')})"
    end = max(ct for *_, ct in jobs if ct is not None)
    last = [j for j, _, _, ct in jobs if ct == end]
    return Round(
        head=head,
        seconds=(end - min(created)).total_seconds(),
        proof_full_last=any(j.get("name") == "proof-full" for j in last),
        legs=[
            Leg((st - rc).total_seconds(), (ct - st).total_seconds())  # type: ignore[operator]
            for _, rc, st, ct in legs
        ]
        if post
        else [],
    )


def load_heads(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FetchError(f"cannot read the head list {path}: {exc}") from exc
    heads: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if not _HEAD.fullmatch(text):
            raise FetchError(f"{path}: {text!r} is not a 40-character commit SHA")
        if text in heads:
            raise FetchError(f"{path}: head {text} is listed more than once; a head is one round")
        heads.append(text)
    return heads


def _first_counting(
    heads: list[str], wanted: int, *, post: bool, shards: int, checks: Checks, side: str
) -> list[Round]:
    counted: list[Round] = []
    for head in heads:
        if len(counted) == wanted:
            break
        outcome = evaluate_head(head, post=post, shards=shards)
        if isinstance(outcome, str):
            checks.note(f"excluded {head} ({side}): {outcome}")
        else:
            counted.append(outcome)
    return counted


def _spread(values: Sequence[float]) -> str:
    return f"{min(values):.0f}-{max(values):.0f} s" if values else "none"


def cmd_rounds(args: argparse.Namespace) -> int:
    if args.rounds < 1:
        raise FetchError("--rounds must be at least 1")
    shards = _shard_count(_contract(args.contract))
    pre_heads, post_heads = load_heads(args.pre), load_heads(args.post)
    shared = sorted(set(pre_heads) & set(post_heads))
    if shared:
        raise FetchError(f"{len(shared)} head(s) are in both lists: {', '.join(shared)}")
    checks = Checks()
    pre = _first_counting(
        pre_heads, args.rounds, post=False, shards=shards, checks=checks, side="pre-merge"
    )
    post = _first_counting(
        post_heads, args.rounds, post=True, shards=shards, checks=checks, side="post-merge"
    )
    checks.note(f"pre-merge rounds: {len(pre)}")
    checks.note(f"post-merge rounds: {len(post)}")
    checks.check(
        len(pre) == args.rounds, f"{args.rounds} counting pre-merge heads", f"found {len(pre)}"
    )
    checks.check(
        len(post) == args.rounds, f"{args.rounds} counting post-merge heads", f"found {len(post)}"
    )
    if not pre or not post:
        return 1
    pre_seconds, post_seconds = [r.seconds for r in pre], [r.seconds for r in post]
    pre_median, post_median = statistics.median(pre_seconds), statistics.median(post_seconds)
    checks.note(f"pre-merge median: {pre_median:.0f} s, range {_spread(pre_seconds)}")
    checks.note(f"post-merge median: {post_median:.0f} s, range {_spread(post_seconds)}")
    last = sum(r.proof_full_last for r in post)
    checks.note(f"proof-full last to finish: {last} of {len(post)}")
    delays = [leg.start_delay for r in post for leg in r.legs]
    durations = [leg.duration for r in post for leg in r.legs]
    spreads = [max(d) - min(d) for d in ([leg.duration for leg in r.legs] for r in post) if d]
    if delays:
        median_delay = statistics.median(delays)
        checks.note(
            f"leg start delay median: {median_delay:.0f} s, range {_spread(delays)} "
            f"(recorded, not asserted; the operator's rule reduces the shard count when it is "
            f"above {START_DELAY_RULE_SECONDS} s)"
        )
        checks.note(
            f"leg duration spread: median per round {statistics.median(spreads):.0f} s, "
            f"legs range {_spread(durations)}"
        )
    checks.check(
        post_median < RESEARCH_MEDIAN_SECONDS,
        f"post-merge median is below {RESEARCH_MEDIAN_SECONDS} s (the research's median)",
        f"{post_median:.0f} s",
    )
    checks.check(
        post_median < pre_median,
        "post-merge median is below the pre-merge median",
        f"{post_median:.0f} s vs {pre_median:.0f} s",
    )
    checks.check(
        last * 2 <= len(post),
        "proof-full was last to finish in at most half of the post-merge rounds",
        f"{last} of {len(post)}",
    )
    return checks.code


# ---------------------------------------------------------------------------
# unsharded
# ---------------------------------------------------------------------------


def cmd_unsharded(args: argparse.Namespace) -> int:
    contract = _contract(args.contract)
    report = _json_file(args.report, str(args.report))
    entries = _gate_entries(report, str(args.report))
    targets = report.get("targets", [])
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        raise FetchError(f"{args.report}: targets must be a list of strings")
    planned = plan_gates(contract, mode="full", targets=targets, shard=None, llm_boundary=False)
    recorded = {str(g.get("id")): g for g in entries}
    checks = Checks()
    checks.check(report.get("ok") is True, "report ok is true", f"ok={report.get('ok')!r}")
    checks.check(
        report.get("mode") == "full", "report is a full-mode run", f"mode={report.get('mode')!r}"
    )
    checks.check(
        report.get("cache_mode") == "no-cache",
        "report is a no-cache run",
        f"cache_mode={report.get('cache_mode')!r}",
    )
    checks.check(
        "shard" in report and report["shard"] is None,
        "shard is null: the run was not a leg of a sharded proof",
        f"shard={report.get('shard', '<missing>')!r}",
    )
    expected_ids = {p.gate.id for p in planned}
    checks.check(
        set(recorded) == expected_ids and len(recorded) == len(entries),
        "the report holds exactly the contract's full-mode gates, once each",
        f"missing {sorted(expected_ids - set(recorded))}, extra {sorted(set(recorded) - expected_ids)}",
    )
    for item in planned:
        gate = recorded.get(item.gate.id)
        if gate is None:
            continue
        want_status = (
            "skip" if item.skip_reason is not None else ("pass" if item.gate.required else None)
        )
        checks.check(
            gate.get("command") == list(item.command),
            f"{item.gate.id} ran the contract's unsharded command",
            f"recorded {gate.get('command')}",
        )
        if want_status is not None:
            checks.check(
                gate.get("status") == want_status,
                f"{item.gate.id} status is {want_status}",
                f"status={gate.get('status')!r}",
            )
        if gate.get("status") == "pass" or item.skip_reason is not None:
            code = gate.get("exit_code")
            checks.check(
                type(code) is int and code == 0,
                f"{item.gate.id} exit code is 0",
                f"exit_code={code!r}",
            )
    checks.note("shard: null" if report.get("shard") is None else f"shard: {report.get('shard')!r}")
    timing = recorded.get("unit_timing")
    checks.note(f"unit_timing: {timing.get('status') if timing else 'missing'}")
    full = recorded.get("unit_full")
    checks.note(f"unit_full command: {full.get('command') if full else 'missing'}")
    return checks.code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live acceptance checks for the sharded proof.")
    sub = parser.add_subparsers(dest="command", required=True)
    default_contract = _ROOT / "proof" / "contract.yaml"

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--contract", type=Path, default=default_contract)
        p.add_argument("--root", type=Path, default=Path.cwd(), help="Checkout of merged main")

    merged = sub.add_parser("merged-run", help="check the merged proof run (task 7.1)")
    merged.add_argument("--run", required=True, help="Proof workflow run id on merged main")
    merged.add_argument(
        "--parent-run", required=True, help="Proof run id of the merge commit's parent"
    )
    merged.add_argument("--parent-rev", required=True, help="Parent revision of the merge commit")
    merged.add_argument("--merged-rev", required=True, help="The merge commit")
    merged.add_argument(
        "--main-branch", default="main", help="Branch the two push runs must have run on"
    )
    common(merged)
    merged.set_defaults(func=cmd_merged_run)

    timing = sub.add_parser("timing-leg", help="check the timing gate in leg 1 (task 7.2)")
    timing.add_argument("--run", required=True)
    common(timing)
    timing.set_defaults(func=cmd_timing_leg)

    rounds = sub.add_parser("rounds", help="compare round times before and after (task 7.3)")
    rounds.add_argument(
        "--pre", type=Path, required=True, help="File of PR head SHAs, newest first"
    )
    rounds.add_argument(
        "--post", type=Path, required=True, help="File of PR head SHAs, newest first"
    )
    rounds.add_argument("--rounds", type=int, default=10)
    common(rounds)
    rounds.set_defaults(func=cmd_rounds)

    unsharded = sub.add_parser("unsharded", help="check a local unsharded report (task 7.4)")
    unsharded.add_argument("--report", type=Path, required=True)
    common(unsharded)
    unsharded.set_defaults(func=cmd_unsharded)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FetchError as exc:
        print(f"live_acceptance: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
