"""Fail-closed aggregate of the legs of the sharded full proof (issue #1307).

``proof.yml`` runs the full proof as N legs (``run.py --mode full --shard K``).
This script is the job named ``proof-full``: it reads every leg's report, ids file
and JUnit file and passes only when every check below holds. It never raises: an
unreadable or wrongly typed input is a reason, and the exit code is 1 when there
is any reason. The merged report is written whether or not the legs passed, so
repair tooling still gets ``failures[]``.

Checks (design D6 of openspec/changes/proof-gate-sharding):

    A0  the leg job result passed in is ``success``
    A1  reports exist for exactly leg indices 1..N (N = the contract's shards)
    A2  each report is a full-mode, no-cache run of this contract, for its leg,
        on the commit the aggregate itself runs on
    A3  each report's gates are the contract's, with the contract's command and
        ``required`` flag for that leg, and the status the contract implies
    A4  each report says ``ok``
    A5  (only when A0-A4 hold) the legs' selections add up to an independently
        collected set: every test exactly once, the timing tests in leg 1
    A6  what each leg executed (its JUnit file) is what it selected

Usage:
    python scripts/proof/aggregate.py --reports DIR --leg-result RESULT \\
        --expected-sha SHA [--report OUT.json] [--json]

Exit codes: 0 every check holds, 1 at least one reason (or an unreadable contract),
2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from defusedxml import ElementTree as SafeElementTree  # noqa: E402

from scripts.ci_shard_union import check_union  # noqa: E402
from scripts.proof.contract import (  # noqa: E402
    ContractError,
    GateSpec,
    ProofContract,
    load_contract,
)
from scripts.proof.runner import (  # noqa: E402
    COLLECTOR_PATH,
    LOG_SLICE_CHARS,
    ShardPosition,
    plan_gates,
    selection_collect_args,
)

REPORT_NAME = "proof-report-full.json"
VALID_STATUSES = frozenset({"pass", "fail", "skip", "blocked"})
MAX_NAMED = 10
COLLECT_TIMEOUT_SECONDS = 300

Collector = Callable[[Sequence[str]], list[str]]


class CollectionError(Exception):
    """The independent collection of the reference test set failed."""


class _InputError(Exception):
    """An input file is missing, unreadable or wrongly typed."""


@dataclass
class AggregateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Leg:
    index: int
    rel: str
    directory: Path
    report: dict[str, Any]


@dataclass
class _JUnit:
    pairs: Counter[tuple[str, str]]
    tests: int
    skipped: int
    failures: int
    errors: int


# ---------------------------------------------------------------------------
# JUnit identity
# ---------------------------------------------------------------------------

_ILLEGAL_XML = re.compile("[^\u0009\u000a\u000d -~\u0080-퟿-�\U00010000-\U0010ffff]")


def _escape_illegal(match: re.Match[str]) -> str:
    code = ord(match.group())
    return f"#x{code:02X}" if code <= 0xFF else f"#x{code:04X}"


def junit_pair(node_id: str) -> tuple[str, str]:
    """The (classname, name) pair pytest writes to JUnit for a test's node id.

    pytest derives it from the id by a fixed rule (``_pytest.junitxml``): the path
    becomes a dotted module, class names follow it, the parameter id stays in the
    name. The rule is not injective, which is why A5 rejects two collected ids that
    give one pair. ``tests/test_proof_aggregate.py`` checks this function against the
    JUnit file real pytest writes.
    """
    path, bracket, params = node_id.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), _ILLEGAL_XML.sub(_escape_illegal, names[-1])


def _pair_text(pair: tuple[str, str]) -> str:
    return f"{pair[0]}::{pair[1]}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _names(items: Iterable[str]) -> str:
    ordered = sorted(items)
    shown = ", ".join(ordered[:MAX_NAMED])
    return shown + (f" (+{len(ordered) - MAX_NAMED} more)" if len(ordered) > MAX_NAMED else "")


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _load_json_object(path: Path, rel: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _InputError(f"{rel}: not readable JSON ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise _InputError(f"{rel}: not a JSON object")
    return value


def _shape_problems(report: Mapping[str, Any]) -> list[str]:
    """Wrong-typed fields of a leg report, as short messages (empty when it is sound)."""
    problems: list[str] = []
    if not _is_int(report.get("schema_version")):
        problems.append("schema_version must be an integer")
    if not isinstance(report.get("ok"), bool):
        problems.append("ok must be a boolean")
    for key in ("mode", "cache_mode", "contract_name", "runner"):
        if not isinstance(report.get(key), str):
            problems.append(f"{key} must be a string")
    if report.get("head_sha") is not None and not isinstance(report.get("head_sha"), str):
        problems.append("head_sha must be a string or null")
    shard = report.get("shard")
    if not (
        isinstance(shard, dict) and _is_int(shard.get("index")) and _is_int(shard.get("count"))
    ):
        problems.append("shard must be an object with integer index and count")
    if not _is_str_list(report.get("targets")):
        problems.append("targets must be a list of strings")
    gates = report.get("gates")
    if not isinstance(gates, list):
        problems.append("gates must be a list")
    else:
        for position, gate in enumerate(gates):
            if not isinstance(gate, dict):
                problems.append(f"gates[{position}] must be an object")
                continue
            if not isinstance(gate.get("id"), str):
                problems.append(f"gates[{position}].id must be a string")
            if not _is_str_list(gate.get("command")):
                problems.append(f"gates[{position}].command must be a list of strings")
            if not isinstance(gate.get("required"), bool):
                problems.append(f"gates[{position}].required must be a boolean")
            if not (isinstance(gate.get("status"), str) and gate["status"] in VALID_STATUSES):
                problems.append(f"gates[{position}].status must be one of {sorted(VALID_STATUSES)}")
            for key in ("exit_code", "duration_ms"):
                if not _is_int(gate.get(key)):
                    problems.append(f"gates[{position}].{key} must be an integer")
    failures = report.get("failures")
    if not isinstance(failures, list) or not all(isinstance(f, dict) for f in failures):
        problems.append("failures must be a list of objects")
    otel = report.get("otel")
    if otel is not None:
        if not isinstance(otel, dict):
            problems.append("otel must be an object or null")
        else:
            spans = otel.get("spans", [])
            if not isinstance(spans, list) or not all(isinstance(s, dict) for s in spans):
                problems.append("otel.spans must be a list of objects")
            if not isinstance(otel.get("resource", {}), dict):
                problems.append("otel.resource must be an object")
    return problems


# ---------------------------------------------------------------------------
# Reading the legs (A1, A2)
# ---------------------------------------------------------------------------


def _discover(reports_dir: Path, exclude: Path | None, reasons: list[str]) -> list[_Leg]:
    """Every readable, well-typed leg report under ``reports_dir``, trusting only ``shard``."""
    if not reports_dir.is_dir():
        reasons.append(f"reports directory {reports_dir} does not exist")
        return []
    skip = exclude.resolve() if exclude is not None else None
    found: list[_Leg] = []
    for path in sorted(reports_dir.rglob(REPORT_NAME)):
        if not path.is_file() or (skip is not None and path.resolve() == skip):
            continue
        rel = path.relative_to(reports_dir).as_posix()
        try:
            report = _load_json_object(path, rel)
            problems = _shape_problems(report)
        except _InputError as exc:
            reasons.append(str(exc))
            continue
        except Exception as exc:  # a report that breaks a check is a reason naming the file
            reasons.append(f"{rel}: could not be checked ({type(exc).__name__}: {exc})")
            continue
        if problems:
            reasons.append(f"{rel}: " + "; ".join(problems))
            continue
        found.append(_Leg(report["shard"]["index"], rel, path.parent, report))
    return found


def _legs_by_index(
    found: list[_Leg], count: int, reasons: list[str]
) -> tuple[dict[int, _Leg], set[int]]:
    """Legs usable for the checks (one report per index in 1..count) and the indexes seen."""
    by_index: dict[int, list[_Leg]] = {}
    for leg in found:
        if not 1 <= leg.index <= count:
            reasons.append(f"{leg.rel}: leg index {leg.index} is outside 1..{count}")
            continue
        by_index.setdefault(leg.index, []).append(leg)
    usable: dict[int, _Leg] = {}
    for index, group in sorted(by_index.items()):
        if len(group) > 1:
            reasons.append(
                f"leg {index}: more than one report ({', '.join(sorted(leg.rel for leg in group))})"
            )
        else:
            usable[index] = group[0]
    return usable, set(by_index)


def _check_identity(
    contract: ProofContract, leg: _Leg, count: int, expected_sha: str, reasons: list[str]
) -> None:
    report = leg.report
    label = f"leg {leg.index}"
    if report["schema_version"] != 1:
        reasons.append(f"{label}: schema_version is {report['schema_version']!r}, expected 1")
    expected = {
        "mode": "full",
        "cache_mode": "no-cache",
        "contract_name": contract.name,
        "runner": contract.runner,
    }
    for key, want in expected.items():
        if report[key] != want:
            reasons.append(f"{label}: {key} is {report[key]!r}, expected {want!r}")
    if report["shard"]["count"] != count:
        reasons.append(
            f"{label}: shard count {report['shard']['count']} differs from the contract's {count}"
        )
    sha = report.get("head_sha")
    if not sha:
        reasons.append(f"{label}: head_sha is missing")
    elif sha != expected_sha:
        reasons.append(f"{label}: head_sha {sha!r} is not the commit under test {expected_sha!r}")
    if report["ok"] is not True:
        reasons.append(f"{label}: report says ok is false")


def _check_gates(contract: ProofContract, leg: _Leg, count: int, reasons: list[str]) -> None:
    """A3: gate ids, commands, required flags and statuses against the contract's plan."""
    label = f"leg {leg.index}"
    planned = plan_gates(
        contract,
        mode="full",
        targets=leg.report["targets"],
        shard=ShardPosition(leg.index, count),
        llm_boundary=False,
    )
    recorded: dict[str, list[dict[str, Any]]] = {}
    for gate in leg.report["gates"]:
        recorded.setdefault(gate["id"], []).append(gate)
    for gate_id, entries in recorded.items():
        if len(entries) > 1:
            reasons.append(f"{label}: gate {gate_id} is recorded more than once")
    planned_ids = {item.gate.id for item in planned}
    for gate_id in sorted(set(recorded) - planned_ids):
        reasons.append(f"{label}: unexpected gate {gate_id} (not in the contract's full mode)")
    for item in planned:
        gate = item.gate
        entries = recorded.get(gate.id)
        if not entries:
            reasons.append(f"{label}: gate {gate.id} is missing from the report")
            continue
        entry = entries[0]
        if entry["command"] != list(item.command):
            reasons.append(
                f"{label}: gate {gate.id} command differs from the contract's for this leg: "
                f"expected {list(item.command)}, recorded {entry['command']}"
            )
        if entry["required"] != gate.required:
            reasons.append(
                f"{label}: gate {gate.id} required flag is {entry['required']}, "
                f"the contract's is {gate.required}"
            )
        status = entry["status"]
        if item.skip_reason is not None and status != "skip":
            reasons.append(
                f"{label}: gate {gate.id} status {status!r}, expected 'skip' ({item.skip_reason})"
            )
        elif item.skip_reason is None and gate.required and status != "pass":
            reasons.append(f"{label}: gate {gate.id} status {status!r}, expected 'pass'")


# ---------------------------------------------------------------------------
# Reading ids and JUnit files (A5, A6)
# ---------------------------------------------------------------------------


def _read_ids(path: Path, rel: str) -> list[str]:
    payload = _load_json_object(path, rel)
    ids = payload.get("ids")
    if not _is_str_list(ids):
        raise _InputError(f"{rel}: 'ids' must be a list of strings")
    return list(ids)  # type: ignore[arg-type]


def _read_junit(path: Path, rel: str) -> _JUnit:
    try:
        root = SafeElementTree.parse(path).getroot()
    except (OSError, ValueError, SafeElementTree.ParseError) as exc:
        raise _InputError(f"{rel}: not readable JUnit XML ({type(exc).__name__})") from exc
    if root is None:
        raise _InputError(f"{rel}: not readable JUnit XML (no root element)")
    pairs: Counter[tuple[str, str]] = Counter()
    skipped = failures = errors = tests = 0
    for case in root.iter("testcase"):
        classname, name = case.get("classname"), case.get("name")
        if classname is None or name is None:
            raise _InputError(f"{rel}: a testcase has no classname or name")
        pairs[(classname, name)] += 1
        tests += 1
        tags = {child.tag for child in case}
        skipped += "skipped" in tags
        failures += "failure" in tags
        errors += "error" in tags
    return _JUnit(pairs, tests, skipped, failures, errors)


def subprocess_collector(root: Path) -> Collector:
    """The default independent collector: ``ci_shard_union.py --emit-ids`` in a subprocess."""

    def collect(args: Sequence[str]) -> list[str]:
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ids.json"
            argv = [sys.executable, str(COLLECTOR_PATH), "--emit-ids", str(out), "--", *args]
            try:
                done = subprocess.run(
                    argv,
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=COLLECT_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CollectionError(f"collection could not run: {exc}") from exc
            if done.returncode != 0:
                tail = (done.stderr or done.stdout).strip()[-500:]
                raise CollectionError(f"collection failed with exit {done.returncode}: {tail}")
            try:
                return _read_ids(out, "collector output")
            except _InputError as exc:
                raise CollectionError(f"collection wrote unusable output: {exc}") from exc

    return collect


# ---------------------------------------------------------------------------
# A5 and A6
# ---------------------------------------------------------------------------


def _accounting(
    contract: ProofContract,
    legs: dict[int, _Leg],
    count: int,
    collector: Collector,
    reasons: list[str],
    junit_sums: dict[int, dict[str, int]],
) -> None:
    started_with = len(reasons)
    gates = contract.gates_for_mode("full")
    split = [g for g in gates if g.shard == "split"]
    leader = [g for g in gates if g.shard == "leader"]
    if not split:
        reasons.append("the contract declares shards but no gate with shard: split")
        return
    try:
        reference = {g.id: collector(selection_collect_args(g.command)) for g in [*split, *leader]}
    except CollectionError as exc:
        reasons.append(f"independent collection failed: {exc}")
        return

    timing: set[str] = set()
    for gate in leader:
        found = set(reference[gate.id])
        if not found:
            reasons.append(f"no timing tests were collected for {gate.id}: it would prove nothing")
        timing |= found
    everything: set[str] = set(timing)
    for gate in split:
        everything |= set(reference[gate.id])

    collisions: dict[tuple[str, str], str] = {}
    for node_id in sorted(everything):
        pair = junit_pair(node_id)
        other = collisions.setdefault(pair, node_id)
        if other != node_id:
            reasons.append(
                f"tests {other!r} and {node_id!r} share the JUnit identity "
                f"{_pair_text(pair)}: a JUnit file could not show which one ran"
            )

    selections: dict[tuple[str, int], list[str]] = {}
    for leg in sorted(legs.values(), key=lambda item: item.index):
        planned = plan_gates(
            contract,
            mode="full",
            targets=leg.report["targets"],
            shard=ShardPosition(leg.index, count),
        )
        ran_here = {p.gate.id for p in planned if p.skip_reason is None}
        for gate in [*split, *leader]:
            if gate.id in ran_here:
                _read_gate_files(leg, gate, selections, reasons, junit_sums)

    for gate in split:
        sets = [set(ids) for (gid, _), ids in selections.items() if gid == gate.id]
        if len(sets) != count:
            continue  # a leg's ids could not be read: that is already a reason
        wanted = set(reference[gate.id]) - timing
        missing, extra, duplicated = check_union(wanted, sets)
        if missing:
            reasons.append(f"tests missing from every leg ({len(missing)}): {_names(missing)}")
        if duplicated:
            reasons.append(
                f"tests selected by more than one leg ({len(duplicated)}): {_names(duplicated)}"
            )
        selected_timing = extra & timing
        if selected_timing:
            reasons.append(f"timing tests selected by {gate.id}: {_names(selected_timing)}")
        not_collected = extra - timing
        if not_collected:
            reasons.append(
                f"tests selected by a leg but not collected ({len(not_collected)}): {_names(not_collected)}"
            )
    for gate in leader:
        ids = selections.get((gate.id, 1))
        if ids is None:
            continue
        wanted_set, got = set(reference[gate.id]), set(ids)
        if got != wanted_set:
            reasons.append(
                f"{gate.id} selection in leg 1 differs from the collected timing tests: "
                f"missing {_names(wanted_set - got) or 'none'}; extra {_names(got - wanted_set) or 'none'}"
            )

    totals = sum(s["tests"] for s in junit_sums.values())
    if len(reasons) == started_with and totals != len(everything):
        reasons.append(
            f"JUnit files record {totals} tests in all, {len(everything)} were collected"
        )


def _read_gate_files(
    leg: _Leg,
    gate: GateSpec,
    selections: dict[tuple[str, int], list[str]],
    reasons: list[str],
    junit_sums: dict[int, dict[str, int]],
) -> None:
    prefix = leg.rel.rsplit("/", 1)[0] + "/" if "/" in leg.rel else ""
    ids_rel, junit_rel = f"{prefix}ids-{gate.id}.json", f"{prefix}junit-{gate.id}.xml"
    label = f"leg {leg.index}"
    ids: list[str] | None = None
    try:
        ids = _read_ids(leg.directory / f"ids-{gate.id}.json", ids_rel)
    except _InputError as exc:
        reasons.append(str(exc))
    junit: _JUnit | None = None
    try:
        junit = _read_junit(leg.directory / f"junit-{gate.id}.xml", junit_rel)
    except _InputError as exc:
        reasons.append(str(exc))
    if ids is not None:
        selections[(gate.id, leg.index)] = ids
        repeated = [i for i, n in Counter(ids).items() if n > 1]
        if repeated:
            reasons.append(f"{label}: {gate.id} selected a test more than once: {_names(repeated)}")
    if junit is None:
        return
    sums = junit_sums.setdefault(leg.index, {"tests": 0, "skipped": 0, "failures": 0, "errors": 0})
    sums["tests"] += junit.tests
    sums["skipped"] += junit.skipped
    sums["failures"] += junit.failures
    sums["errors"] += junit.errors
    if junit.failures + junit.errors:
        reasons.append(
            f"{label}: {gate.id} JUnit file records {junit.failures + junit.errors} failure or error case(s)"
        )
    if ids is None:
        return
    expected = Counter(junit_pair(i) for i in ids)
    recorded_only = junit.pairs - expected
    selected_only = expected - junit.pairs
    if recorded_only:
        reasons.append(
            f"{label}: {gate.id} JUnit file records tests it did not select "
            f"(recorded but not selected): {_names(_pair_text(p) for p in recorded_only.elements())}"
        )
    if selected_only:
        reasons.append(
            f"{label}: {gate.id} JUnit file lacks tests it selected "
            f"(selected but not recorded): {_names(_pair_text(p) for p in selected_only.elements())}"
        )


# ---------------------------------------------------------------------------
# The merged report
# ---------------------------------------------------------------------------

_SEVERITY = {"fail": 3, "blocked": 2, "pass": 1, "skip": 0}


def _merge_status(statuses: list[str]) -> str:
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    if all(s == "skip" for s in statuses):
        return "skip"
    return "pass"


def _merged_report(
    contract: ProofContract,
    legs: dict[int, _Leg],
    reasons: list[str],
    junit_sums: dict[int, dict[str, int]],
    accounting_ran: bool,
) -> dict[str, Any]:
    ordered = [legs[i] for i in sorted(legs)]
    gates: list[dict[str, Any]] = []
    for spec in contract.gates_for_mode("full", llm_boundary=True):
        entries = [
            (leg.index, g) for leg in ordered for g in leg.report["gates"] if g["id"] == spec.id
        ]
        if not entries:
            continue
        status = _merge_status([g["status"] for _, g in entries])
        template = next((g for _, g in entries if g["status"] == status), entries[0][1])
        merged = dict(template)
        merged["status"] = status
        merged["duration_ms"] = max(g["duration_ms"] for _, g in entries)
        gates.append(merged)

    failures: list[dict[str, Any]] = []
    for leg in ordered:
        for failure in leg.report["failures"]:
            failures.append({**failure, "shard": leg.index})
    for reason in reasons:
        failures.append(
            {
                "gate": "shard_aggregate",
                "command": [],
                "file": None,
                "test": None,
                "exit_code": 1,
                "log_slice": reason[:LOG_SLICE_CHARS],
                "classify": "test",
                "status": "fail",
                "required": True,
                "shard": None,
            }
        )

    otel: dict[str, Any] | None = None
    if any(leg.report["otel"] is not None for leg in ordered):
        resource = next(
            (leg.report["otel"].get("resource") for leg in ordered if leg.report["otel"]), None
        ) or {
            "service.name": "verdict-proof",
            "proof.contract": contract.name,
            "proof.mode": "full",
        }
        spans = []
        for leg in ordered:
            for span in (leg.report["otel"] or {}).get("spans", []):
                attributes = dict(span.get("attributes") or {})
                attributes["proof.shard"] = leg.index
                spans.append({**span, "attributes": attributes})
        otel = {"resource": resource, "spans": spans}

    shas = {leg.report["head_sha"] for leg in ordered if leg.report.get("head_sha")}
    shards = []
    for leg in ordered:
        entry: dict[str, Any] = {
            "index": leg.index,
            "ok": leg.report["ok"],
            "gates": {g["id"]: g["status"] for g in leg.report["gates"]},
            "junit": dict(junit_sums[leg.index]) if leg.index in junit_sums else None,
        }
        if entry["junit"] is None:
            entry["junit_reason"] = (
                "JUnit accounting did not run: an earlier check failed"
                if not accounting_ran
                else "the leg's JUnit file could not be read"
            )
        shards.append(entry)

    return {
        "schema_version": 1,
        "ok": not reasons,
        "mode": "full",
        "cache_mode": "no-cache",
        "runner": contract.runner,
        "contract_name": contract.name,
        "head_sha": next(iter(shas)) if len(shas) == 1 else None,
        "gates": gates,
        "failures": failures,
        "otel": otel,
        "shards": shards,
    }


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def aggregate(
    contract: ProofContract,
    *,
    reports_dir: Path,
    leg_result: str,
    expected_sha: str,
    collector: Collector,
    exclude: Path | None = None,
) -> AggregateResult:
    """Check every leg and build the merged report. Never raises."""
    reasons: list[str] = []
    legs: dict[int, _Leg] = {}
    junit_sums: dict[int, dict[str, int]] = {}
    accounting_ran = False
    try:
        if leg_result != "success":
            reasons.append(f"proof-shard job result is {leg_result!r}, not 'success'")
        if not expected_sha:
            reasons.append("the commit under test is empty or unknown")
        count = contract.shards
        found = _discover(reports_dir, exclude, reasons)
        if count is None:
            reasons.append("the contract declares no shards (modes.full.shards)")
        else:
            legs, seen = _legs_by_index(found, count, reasons)
            for index in range(1, count + 1):
                if index not in seen:
                    reasons.append(f"leg {index}: report is missing")
            for leg in legs.values():
                _check_identity(contract, leg, count, expected_sha, reasons)
                _check_gates(contract, leg, count, reasons)
            shas = {leg.report["head_sha"] for leg in legs.values() if leg.report.get("head_sha")}
            if len(shas) > 1:
                reasons.append(f"legs ran on different commits: {_names(shas)}")
            targets = {tuple(leg.report["targets"]) for leg in legs.values()}
            if len(targets) > 1:
                reasons.append(f"legs recorded different targets: {sorted(targets)}")
            if not reasons:
                accounting_ran = True
                _accounting(contract, legs, count, collector, reasons, junit_sums)
    except Exception as exc:
        reasons.append(f"internal error: {type(exc).__name__}: {exc}")
    try:
        report = _merged_report(contract, legs, reasons, junit_sums, accounting_ran)
    except Exception as exc:
        reasons.append(f"internal error while merging: {type(exc).__name__}: {exc}")
        report = _minimal_report(contract.name, reasons)
    return AggregateResult(ok=not reasons, reasons=reasons, report=report)


def _minimal_report(contract_name: str, reasons: list[str]) -> dict[str, Any]:
    """A failed report in today's shape for when nothing could be merged."""
    return {
        "schema_version": 1,
        "ok": False,
        "mode": "full",
        "cache_mode": "no-cache",
        "runner": "native",
        "contract_name": contract_name,
        "head_sha": None,
        "gates": [],
        "failures": [
            {
                "gate": "shard_aggregate",
                "command": [],
                "file": None,
                "test": None,
                "exit_code": 1,
                "log_slice": reason[:LOG_SLICE_CHARS],
                "classify": "test",
                "status": "fail",
                "required": True,
                "shard": None,
            }
            for reason in reasons
        ],
        "otel": None,
        "shards": [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate the legs of the sharded full proof.")
    parser.add_argument("--reports", type=Path, required=True, help="Directory holding the legs")
    parser.add_argument("--leg-result", required=True, help="Result of the proof-shard job")
    parser.add_argument("--expected-sha", required=True, help="Commit the legs must have run on")
    parser.add_argument("--contract", type=Path, default=_ROOT / "proof" / "contract.yaml")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="Checkout to collect tests in"
    )
    parser.add_argument("--report", type=Path, help="Write the merged report here")
    parser.add_argument("--json", action="store_true", help="Print the merged report to stdout")
    return parser


def main(argv: list[str] | None = None, *, collector: Collector | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract(args.contract)
    except ContractError as exc:
        reasons = [f"the proof contract could not be read: {exc}"]
        result = AggregateResult(
            ok=False, reasons=reasons, report=_minimal_report("unknown", reasons)
        )
    else:
        result = aggregate(
            contract,
            reports_dir=args.reports,
            leg_result=args.leg_result,
            expected_sha=args.expected_sha,
            collector=collector or subprocess_collector(args.root),
            exclude=args.report,
        )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(result.report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(result.report, indent=2, sort_keys=True))
    else:
        legs = len(result.report.get("shards", []))
        print(f"proof-full aggregate {'PASS' if result.ok else 'FAIL'} legs={legs}")
        for reason in result.reasons:
            print(f"  - {reason}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
