"""Tests for the fail-closed aggregate of the sharded full proof (issue #1307).

One builder writes a complete, valid set of legs (reports written by the real
ProofRunner, ids files, JUnit files). Every negative test is one mutation of that
set and asserts the reason of the check it targets, so deleting a check turns its
test red. The checks that need an independently collected test set use a stub
collector; the default collector and the JUnit identity rule are tested against
real pytest on a toy tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import pytest
import yaml

from scripts.proof.aggregate import AggregateResult, CollectionError, aggregate, junit_pair, main
from scripts.proof.contract import ProofContract, load_contract
from scripts.proof.runner import CommandResult, ProofRunner

SHA = "c0ffee" * 6 + "c0ff"
TARGETS = ("tests/test_proof_pipeline.py",)
NON_TIMING = [f"tests/test_mod{m}.py::test_case[{i}]" for m in range(3) for i in range(3)]
TIMING = ["tests/test_timing.py::test_a", "tests/test_timing.py::TestT::test_b"]
UNIT_FULL = ["pytest", "tests/", "-q", "--tb=short"]
UNIT_TIMING = [*UNIT_FULL, "-m", "timing", "-p", "no:xdist"]


def _payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "fixture-repo",
        "runner": "native",
        "modes": {
            "targeted": {"phases": ["format", "unit_targeted"]},
            "full": {
                "phases": [
                    "format",
                    "unit_targeted",
                    "unit_full",
                    "unit_timing",
                    "security",
                    "adversarial",
                ],
                "shards": 3,
            },
        },
        "gates": [
            {"id": "format", "phase": "format", "classify": "format", "command": ["echo", "fmt"]},
            {
                "id": "unit_targeted",
                "phase": "unit_targeted",
                "classify": "test",
                "command": ["pytest", "{targets}", "-q", "--tb=short"],
                "targets_placeholder": "{targets}",
            },
            {
                "id": "unit_full",
                "phase": "unit_full",
                "classify": "test",
                "command": list(UNIT_FULL),
                "shard": "split",
            },
            {
                "id": "unit_timing",
                "phase": "unit_timing",
                "classify": "test",
                "command": list(UNIT_TIMING),
                "shard": "leader",
                "env_clear": ["PYTEST_ADDOPTS"],
            },
            {
                "id": "security_bandit",
                "phase": "security",
                "classify": "security",
                "command": ["echo", "bandit"],
                "required": False,
            },
            {
                "id": "promptfoo",
                "phase": "adversarial",
                "classify": "adversarial",
                "command": ["echo", "promptfoo"],
                "when": "llm_boundary",
                "required": False,
            },
        ],
    }


def _contract(tmp_path: Path) -> ProofContract:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(_payload()), encoding="utf-8")
    return load_contract(path)


def _plain_pair(node_id: str) -> tuple[str, str]:
    """JUnit identity of a plain id, written independently of the code under test."""
    head, bracket, tail = node_id.partition("[")
    parts = head.split("::")
    module = parts[0].removesuffix(".py").replace("/", ".")
    return ".".join([module, *parts[1:-1]]), parts[-1] + bracket + tail


def _junit_xml(
    cases: Sequence[tuple[str, str]],
    *,
    skipped: Sequence[int] = (),
    failing: Sequence[int] = (),
    erroring: Sequence[int] = (),
) -> str:
    rows = []
    for position, (classname, name) in enumerate(cases):
        attrs = f"classname={quoteattr(classname)} name={quoteattr(name)}"
        if position in failing:
            rows.append(
                f"<testcase {attrs}><failure message={quoteattr('x')}>{escape('x')}</failure></testcase>"
            )
        elif position in erroring:
            rows.append(
                f"<testcase {attrs}><error message={quoteattr('x')}>{escape('x')}</error></testcase>"
            )
        elif position in skipped:
            rows.append(f'<testcase {attrs}><skipped type="pytest.skip" message="s"/></testcase>')
        else:
            rows.append(f"<testcase {attrs}/>")
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{len(erroring)}" failures="{len(failing)}" '
        f'skipped="{len(skipped)}" tests="{len(cases)}">{"".join(rows)}</testsuite></testsuites>'
    )


class _LegExecutor:
    """Plays the collector for the runner (writing the ids it was given) and passes gates."""

    def __init__(self, ids_by_gate: dict[str, list[str]]) -> None:
        self.ids_by_gate = ids_by_gate

    def __call__(self, argv: list[str], *, env: dict[str, str], cwd: Path) -> CommandResult:
        del env, cwd
        if len(argv) > 1 and Path(argv[1]).name == "ci_shard_union.py":
            target = Path(argv[argv.index("--emit-ids") + 1])
            gate = target.name.removeprefix("ids-").removesuffix(".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            ids = self.ids_by_gate[gate]
            target.write_text(json.dumps({"schema_version": 1, "count": len(ids), "ids": ids}))
        return CommandResult(0, "", "")


class _Collector:
    """The aggregate's independent collection: F for no marker, T for ``-m timing``."""

    def __init__(self, everything: list[str], timing: list[str]) -> None:
        self.everything = everything
        self.timing = timing
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> list[str]:
        self.calls.append(list(args))
        if "-m" in args and args[list(args).index("-m") + 1] == "timing":
            return list(self.timing)
        return list(self.everything)


class Fixture:
    """A valid set of legs on disk plus everything needed to aggregate them."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        non_timing: list[str] | None = None,
        timing: list[str] | None = None,
        targets: Sequence[str] = TARGETS,
        per_leg: list[list[str]] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.contract = _contract(tmp_path)
        self.non_timing = list(NON_TIMING if non_timing is None else non_timing)
        self.timing = list(TIMING if timing is None else timing)
        self.reports = tmp_path / "reports"
        self.reports.mkdir()
        groups = per_leg or [self.non_timing[k::3] for k in range(3)]
        for leg in (1, 2, 3):
            self._build_leg(leg, groups[leg - 1], list(targets))
        self.collector = _Collector([*self.non_timing, *self.timing], self.timing)

    def _build_leg(self, leg: int, unit_full_ids: list[str], targets: list[str]) -> None:
        work = self.tmp_path / "work" / f"leg{leg}"
        work.mkdir(parents=True)
        executor = _LegExecutor({"unit_full": unit_full_ids, "unit_timing": self.timing})
        report = ProofRunner(self.contract, root=work, executor=executor).run(
            mode="full", targets=targets, no_cache=True, otel=True, shard=leg
        )
        payload = report.to_dict()
        payload["head_sha"] = SHA
        dest = self.reports / f"proof-shard-{leg}"
        shutil.copytree(work / f"proof-shard-{leg}", dest)
        (dest / "proof-report-full.json").write_text(json.dumps(payload), encoding="utf-8")
        (dest / "junit-unit_full.xml").write_text(
            _junit_xml([_plain_pair(i) for i in unit_full_ids]), encoding="utf-8"
        )
        if leg == 1:
            (dest / "junit-unit_timing.xml").write_text(
                _junit_xml([_plain_pair(i) for i in self.timing]), encoding="utf-8"
            )

    def leg_dir(self, leg: int) -> Path:
        return self.reports / f"proof-shard-{leg}"

    def edit_report(self, leg: int, edit: Callable[[dict[str, Any]], None]) -> None:
        path = self.leg_dir(leg) / "proof-report-full.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        edit(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def edit_gate(self, leg: int, gate_id: str, edit: Callable[[dict[str, Any]], None]) -> None:
        def apply(payload: dict[str, Any]) -> None:
            edit(next(g for g in payload["gates"] if g["id"] == gate_id))

        self.edit_report(leg, apply)

    def edit_ids(self, leg: int, gate: str, edit: Callable[[list[str]], None]) -> None:
        path = self.leg_dir(leg) / f"ids-{gate}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        edit(payload["ids"])
        payload["count"] = len(payload["ids"])
        path.write_text(json.dumps(payload), encoding="utf-8")

    def write_junit(self, leg: int, gate: str, text: str) -> None:
        (self.leg_dir(leg) / f"junit-{gate}.xml").write_text(text, encoding="utf-8")

    def run(self, *, leg_result: str = "success", sha: str = SHA) -> AggregateResult:
        return aggregate(
            self.contract,
            reports_dir=self.reports,
            leg_result=leg_result,
            expected_sha=sha,
            collector=self.collector,
        )


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def reasons_with(result: AggregateResult, *needles: str) -> list[str]:
    return [r for r in result.reasons if all(n in r for n in needles)]


def assert_fails_with(result: AggregateResult, *needles: str) -> None:
    assert result.ok is False
    assert reasons_with(result, *needles), (needles, result.reasons)
    assert result.report["ok"] is False
    assert any(
        f["gate"] == "shard_aggregate" and all(n in f["log_slice"] for n in needles)
        for f in result.report["failures"]
    ), result.report["failures"]


# ---------------------------------------------------------------------------
# The passing case
# ---------------------------------------------------------------------------


def test_a_complete_valid_set_of_legs_passes(fx: Fixture) -> None:
    result = fx.run()
    assert result.reasons == []
    assert result.ok is True
    report = result.report
    assert report["ok"] is True
    assert [s["index"] for s in report["shards"]] == [1, 2, 3]
    assert all(s["ok"] is True for s in report["shards"])
    assert sum(s["junit"]["tests"] for s in report["shards"]) == len(NON_TIMING) + len(TIMING)
    assert report["shards"][0]["junit"]["tests"] == 3 + len(TIMING)
    assert report["shards"][1]["junit"]["tests"] == 3


# ---------------------------------------------------------------------------
# A0, A1, A2, A4: presence and identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["failure", "cancelled", "skipped", ""])
def test_a_leg_job_result_other_than_success_fails(fx: Fixture, value: str) -> None:
    result = fx.run(leg_result=value)
    assert_fails_with(result, "proof-shard job result", repr(value))


def test_a_cancelled_run_with_a_missing_leg_names_both(fx: Fixture) -> None:
    shutil.rmtree(fx.leg_dir(2))
    result = fx.run(leg_result="cancelled")
    assert_fails_with(result, "proof-shard job result", "'cancelled'")
    assert_fails_with(result, "leg 2", "missing")


def test_an_absent_report_fails_and_names_the_leg(fx: Fixture) -> None:
    (fx.leg_dir(3) / "proof-report-full.json").unlink()
    assert_fails_with(fx.run(), "leg 3", "missing")


def test_one_leg_too_few_fails(fx: Fixture) -> None:
    shutil.rmtree(fx.leg_dir(1))
    result = fx.run()
    assert_fails_with(result, "leg 1", "missing")
    assert not reasons_with(result, "leg 2", "missing")


def test_one_leg_too_many_fails(fx: Fixture) -> None:
    extra = fx.reports / "proof-shard-4"
    shutil.copytree(fx.leg_dir(3), extra)
    payload_path = extra / "proof-report-full.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["shard"] = {"index": 4, "count": 3}
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    assert_fails_with(fx.run(), "proof-shard-4", "outside 1..3")


def test_a_repeated_leg_index_fails(fx: Fixture) -> None:
    shutil.copytree(fx.leg_dir(2), fx.reports / "elsewhere")
    assert_fails_with(fx.run(), "leg 2", "more than one report")


@pytest.mark.parametrize("index", [0, -1, 4, 99])
def test_a_leg_index_outside_the_declared_range_fails(fx: Fixture, index: int) -> None:
    fx.edit_report(2, lambda p: p.__setitem__("shard", {"index": index, "count": 3}))
    result = fx.run()
    assert_fails_with(result, "proof-shard-2", f"{index}", "outside 1..3")
    assert_fails_with(result, "leg 2", "missing")


def test_a_leg_count_different_from_the_contract_fails(fx: Fixture) -> None:
    fx.edit_report(2, lambda p: p.__setitem__("shard", {"index": 2, "count": 2}))
    assert_fails_with(fx.run(), "leg 2", "shard count 2", "contract's 3")


def test_mixed_head_shas_fail(fx: Fixture) -> None:
    fx.edit_report(2, lambda p: p.__setitem__("head_sha", "d" * 40))
    result = fx.run()
    assert_fails_with(result, "leg 2", "head_sha", "commit under test")
    assert_fails_with(result, "different commits")


def test_a_head_sha_other_than_the_commit_under_test_fails(fx: Fixture) -> None:
    assert_fails_with(fx.run(sha="e" * 40), "head_sha", "commit under test")


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_or_empty_head_sha_fails(fx: Fixture, value: object) -> None:
    fx.edit_report(1, lambda p: p.__setitem__("head_sha", value))
    assert_fails_with(fx.run(), "leg 1", "head_sha")


def test_an_empty_expected_commit_fails_even_when_the_legs_agree(fx: Fixture) -> None:
    for leg in (1, 2, 3):
        fx.edit_report(leg, lambda p: p.__setitem__("head_sha", ""))
    assert_fails_with(fx.run(sha=""), "commit under test")


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("mode", "targeted", "mode is 'targeted'"),
        ("cache_mode", "cache", "cache_mode is 'cache'"),
        ("contract_name", "other", "contract_name is 'other'"),
        ("schema_version", 2, "schema_version"),
        ("runner", "dagger", "runner is 'dagger'"),
    ],
)
def test_a_report_of_the_wrong_kind_fails(
    fx: Fixture, field: str, value: object, needle: str
) -> None:
    fx.edit_report(3, lambda p: p.__setitem__(field, value))
    assert_fails_with(fx.run(), "leg 3", needle)


def test_a_leg_that_reports_not_ok_fails_and_its_failures_are_kept(fx: Fixture) -> None:
    def fail(payload: dict[str, Any]) -> None:
        payload["ok"] = False
        payload["failures"] = [
            {
                "gate": "unit_full",
                "command": [],
                "file": None,
                "test": None,
                "exit_code": 1,
                "log_slice": "boom",
                "classify": "test",
                "status": "fail",
                "required": True,
            }
        ]
        for gate in payload["gates"]:
            if gate["id"] == "unit_full":
                gate["status"] = "fail"
                gate["exit_code"] = 1

    fx.edit_report(2, fail)
    result = fx.run()
    assert_fails_with(result, "leg 2", "ok is false")
    assert_fails_with(result, "leg 2", "unit_full", "status 'fail'")
    kept = [f for f in result.report["failures"] if f["gate"] == "unit_full"]
    assert kept and kept[0]["shard"] == 2
    assert kept[0]["log_slice"] == "boom"
    assert next(g for g in result.report["gates"] if g["id"] == "unit_full")["status"] == "fail"
    assert result.report["shards"][1]["ok"] is False


# ---- unreadable and wrong-typed reports: a reason naming the file, nothing raised ----


def _bad_json(path: Path) -> None:
    path.write_text("{not json", encoding="utf-8")


def _not_object(path: Path) -> None:
    path.write_text("[1, 2, 3]", encoding="utf-8")


def _empty(path: Path) -> None:
    path.write_text("", encoding="utf-8")


def _binary(path: Path) -> None:
    path.write_bytes(b"\xff\xfe\x00\x01")


@pytest.mark.parametrize("corrupt", [_bad_json, _not_object, _empty, _binary])
def test_an_unreadable_report_is_a_reason_that_names_the_file(
    fx: Fixture, corrupt: Callable[[Path], None]
) -> None:
    path = fx.leg_dir(2) / "proof-report-full.json"
    corrupt(path)
    result = fx.run()
    assert_fails_with(result, "proof-shard-2/proof-report-full.json")
    assert fx.collector.calls == []


def _set(key: str, value: object) -> Callable[[dict[str, Any]], None]:
    return lambda p: p.__setitem__(key, value)


def _bad_gate_entry(payload: dict[str, Any]) -> None:
    payload["gates"][0] = "not an object"


def _bad_gate_command(payload: dict[str, Any]) -> None:
    payload["gates"][0]["command"] = "echo fmt"


def _bad_gate_status(payload: dict[str, Any]) -> None:
    payload["gates"][0]["status"] = ["pass"]


def _bad_gate_required(payload: dict[str, Any]) -> None:
    payload["gates"][0]["required"] = "yes"


def _bad_failure_entry(payload: dict[str, Any]) -> None:
    payload["failures"] = ["not an object"]


def _bad_otel_span(payload: dict[str, Any]) -> None:
    payload["otel"]["spans"] = ["not an object"]


@pytest.mark.parametrize(
    "edit",
    [
        _set("shard", None),
        _set("head_sha", 7),
        _set("schema_version", True),
        _set("shard", "2"),
        _set("shard", {"index": "2", "count": 3}),
        _set("shard", {"index": True, "count": 3}),
        _set("shard", {"index": 2}),
        _set("gates", {"id": "x"}),
        _set("gates", None),
        _set("failures", {"gate": "x"}),
        _set("otel", ["x"]),
        _set("ok", "true"),
        _set("targets", "tests/test_x.py"),
        _set("targets", [1]),
        _bad_gate_entry,
        _bad_gate_command,
        _bad_gate_status,
        _bad_gate_required,
        _bad_failure_entry,
        _bad_otel_span,
    ],
)
def test_a_wrong_typed_report_field_is_a_reason_that_names_the_file(
    fx: Fixture, edit: Callable[[dict[str, Any]], None]
) -> None:
    fx.edit_report(2, edit)
    result = fx.run()
    assert result.ok is False
    assert reasons_with(result, "proof-shard-2/proof-report-full.json") or reasons_with(
        result, "leg 2"
    )
    assert fx.collector.calls == []
    assert result.report["ok"] is False


def test_a_leg_with_a_malformed_report_contributes_nothing_to_the_merged_report(
    fx: Fixture,
) -> None:
    fx.edit_report(2, _set("failures", [{"gate": "SECRET-LEAK", "log_slice": "do not merge"}]))
    fx.edit_report(2, _set("gates", "oops"))
    result = fx.run()
    assert result.ok is False
    assert "SECRET-LEAK" not in json.dumps(result.report)
    assert [s["index"] for s in result.report["shards"]] == [1, 3]


# ---------------------------------------------------------------------------
# A3: gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param([*UNIT_FULL, "--splits", "3", "--group", "2"], id="no-marker-filter"),
        pytest.param(
            [*UNIT_FULL, "-m", "not timing", "--group", "2", "--junitxml=proof-shard-2/j.xml"],
            id="no-splits",
        ),
        pytest.param(
            [
                *UNIT_FULL,
                "-m",
                "not timing",
                "--splits",
                "3",
                "--group",
                "2",
                "--splitting-algorithm",
                "least_duration",
                "--junitxml=proof-shard-2/junit-unit_full.xml",
                "--deselect",
                "tests/test_mod0.py::test_case[0]",
            ],
            id="extra-deselect",
        ),
        pytest.param(list(UNIT_FULL), id="unsharded-command"),
    ],
)
def test_an_altered_unit_full_command_fails(fx: Fixture, command: list[str]) -> None:
    fx.edit_gate(2, "unit_full", lambda g: g.__setitem__("command", command))
    assert_fails_with(fx.run(), "leg 2", "unit_full", "command")


def test_a_flipped_required_flag_fails(fx: Fixture) -> None:
    fx.edit_gate(2, "unit_full", lambda g: g.__setitem__("required", False))
    assert_fails_with(fx.run(), "leg 2", "unit_full", "required")


def test_a_flipped_required_flag_on_a_non_required_gate_fails_too(fx: Fixture) -> None:
    fx.edit_gate(1, "security_bandit", lambda g: g.__setitem__("required", True))
    assert_fails_with(fx.run(), "leg 1", "security_bandit", "required")


def test_a_missing_gate_fails(fx: Fixture) -> None:
    fx.edit_report(
        2, lambda p: p.__setitem__("gates", [g for g in p["gates"] if g["id"] != "format"])
    )
    assert_fails_with(fx.run(), "leg 2", "format", "missing")


def test_an_extra_gate_fails(fx: Fixture) -> None:
    def add(payload: dict[str, Any]) -> None:
        payload["gates"].append({**payload["gates"][0], "id": "bonus"})

    fx.edit_report(2, add)
    assert_fails_with(fx.run(), "leg 2", "bonus", "unexpected")


def test_a_repeated_gate_entry_fails(fx: Fixture) -> None:
    fx.edit_report(2, lambda p: p["gates"].append(dict(p["gates"][0])))
    assert_fails_with(fx.run(), "leg 2", "format", "more than once")


def test_unit_timing_absent_in_leg_one_fails(fx: Fixture) -> None:
    fx.edit_report(
        1, lambda p: p.__setitem__("gates", [g for g in p["gates"] if g["id"] != "unit_timing"])
    )
    assert_fails_with(fx.run(), "leg 1", "unit_timing", "missing")


def test_unit_timing_run_in_leg_two_fails(fx: Fixture) -> None:
    def ran(gate: dict[str, Any]) -> None:
        gate["status"] = "pass"
        gate["command"] = [*UNIT_TIMING, "--junitxml=proof-shard-2/junit-unit_timing.xml"]

    fx.edit_gate(2, "unit_timing", ran)
    assert_fails_with(fx.run(), "leg 2", "unit_timing")


def test_unit_timing_skipped_in_leg_one_fails(fx: Fixture) -> None:
    fx.edit_gate(1, "unit_timing", lambda g: g.__setitem__("status", "skip"))
    assert_fails_with(fx.run(), "leg 1", "unit_timing", "status 'skip'", "expected 'pass'")


def test_unit_timing_skipped_in_a_later_leg_is_authorized(fx: Fixture) -> None:
    for leg in (2, 3):
        gate = next(g for g in _report(fx, leg)["gates"] if g["id"] == "unit_timing")
        assert gate["status"] == "skip"
    assert fx.run().ok is True


def _report(fx: Fixture, leg: int) -> dict[str, Any]:
    return json.loads((fx.leg_dir(leg) / "proof-report-full.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("leg", "gate_id"),
    [(1, "format"), (2, "unit_full"), (3, "unit_targeted"), (2, "security_bandit")],
)
def test_a_gate_recorded_as_passed_with_a_nonzero_exit_code_fails(
    fx: Fixture, leg: int, gate_id: str
) -> None:
    """The runner calls a gate passed only for exit 0: a report that says both is not trusted."""
    fx.edit_gate(leg, gate_id, lambda g: g.__setitem__("exit_code", 1))
    assert_fails_with(fx.run(), f"leg {leg}", gate_id, "status 'pass'", "exit code 1")


@pytest.mark.parametrize(("leg", "gate_id"), [(2, "unit_timing"), (1, "promptfoo")])
def test_a_gate_recorded_as_skipped_by_the_contract_with_a_nonzero_exit_code_fails(
    fx: Fixture, leg: int, gate_id: str
) -> None:
    fx.edit_gate(leg, gate_id, lambda g: g.__setitem__("exit_code", 127))
    assert_fails_with(fx.run(), f"leg {leg}", gate_id, "exit code 127")


def test_a_failing_gate_with_its_real_nonzero_exit_code_is_not_a_second_reason(fx: Fixture) -> None:
    """A gate that failed honestly says fail with a nonzero code: only the status is reported."""
    fx.edit_gate(2, "unit_full", lambda g: g.update(status="fail", exit_code=1))
    result = fx.run()
    assert not reasons_with(result, "leg 2", "unit_full", "exit code")


@pytest.mark.parametrize("status", ["fail", "blocked", "skip"])
def test_a_required_gate_that_did_not_pass_fails(fx: Fixture, status: str) -> None:
    fx.edit_gate(3, "format", lambda g: g.__setitem__("status", status))
    assert_fails_with(fx.run(), "leg 3", "format", f"status '{status}'")


def test_an_unrecognised_status_fails(fx: Fixture) -> None:
    fx.edit_gate(3, "format", lambda g: g.__setitem__("status", "ok"))
    assert fx.run().ok is False


def test_unit_targeted_skipped_because_no_targets_were_given_is_authorized(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, targets=())
    for leg in (1, 2, 3):
        gate = next(g for g in _report(fixture, leg)["gates"] if g["id"] == "unit_targeted")
        assert gate["status"] == "skip"
        assert _report(fixture, leg)["targets"] == []
    assert fixture.run().ok is True


def test_unit_targeted_skipped_although_targets_were_given_fails(fx: Fixture) -> None:
    fx.edit_gate(2, "unit_targeted", lambda g: g.__setitem__("status", "skip"))
    assert_fails_with(fx.run(), "leg 2", "unit_targeted", "status 'skip'")


def test_targets_that_differ_between_legs_fail(fx: Fixture) -> None:
    fx.edit_report(2, _set("targets", ["tests/test_other.py"]))
    result = fx.run()
    assert_fails_with(result, "different targets")
    assert_fails_with(result, "leg 2", "unit_targeted", "command")


def test_a_unit_targeted_command_that_does_not_match_the_targets_fails(fx: Fixture) -> None:
    fx.edit_gate(
        3,
        "unit_targeted",
        lambda g: g.__setitem__("command", ["pytest", "tests/", "-q", "--tb=short"]),
    )
    assert_fails_with(fx.run(), "leg 3", "unit_targeted", "command")


def test_a_gate_that_needs_llm_boundary_must_not_have_run(fx: Fixture) -> None:
    fx.edit_gate(2, "promptfoo", lambda g: g.__setitem__("status", "pass"))
    assert_fails_with(fx.run(), "leg 2", "promptfoo", "status 'pass'", "expected 'skip'")


def test_a_gate_the_contract_skips_must_be_recorded_skipped_even_if_not_required(
    fx: Fixture,
) -> None:
    fx.edit_gate(3, "unit_timing", lambda g: g.__setitem__("required", False))
    result = fx.run()
    assert_fails_with(result, "leg 3", "unit_timing", "required")


def test_a_failing_non_required_gate_keeps_the_proof_green(fx: Fixture) -> None:
    def fail(payload: dict[str, Any]) -> None:
        for gate in payload["gates"]:
            if gate["id"] == "security_bandit":
                gate["status"] = "fail"
                gate["exit_code"] = 1
        payload["failures"] = [
            {
                "gate": "security_bandit",
                "command": ["echo", "bandit"],
                "file": None,
                "test": None,
                "exit_code": 1,
                "log_slice": "finding",
                "classify": "security",
                "status": "fail",
                "required": False,
            }
        ]

    fx.edit_report(2, fail)
    result = fx.run()
    assert result.ok is True
    assert result.reasons == []
    assert result.report["ok"] is True
    assert (
        next(g for g in result.report["gates"] if g["id"] == "security_bandit")["status"] == "fail"
    )
    assert [f["gate"] for f in result.report["failures"]] == ["security_bandit"]


def test_a_skipped_non_required_gate_keeps_the_proof_green(fx: Fixture) -> None:
    for leg in (1, 2, 3):
        fx.edit_gate(leg, "security_bandit", lambda g: g.__setitem__("status", "skip"))
    result = fx.run()
    assert result.ok is True
    assert (
        next(g for g in result.report["gates"] if g["id"] == "security_bandit")["status"] == "skip"
    )


# ---------------------------------------------------------------------------
# A5: every collected test runs exactly once
# ---------------------------------------------------------------------------


def test_the_reference_sets_are_collected_with_the_contracts_own_arguments(fx: Fixture) -> None:
    fx.run()
    assert fx.collector.calls == [
        ["tests/", "-q", "--tb=short"],
        ["tests/", "-q", "--tb=short", "-m", "timing", "-p", "no:xdist"],
    ]


def test_a_test_missing_from_every_leg_fails_and_is_named(fx: Fixture) -> None:
    lost = NON_TIMING[4]
    fx.edit_ids(2, "unit_full", lambda ids: ids.remove(lost))
    fx.write_junit(
        2, "unit_full", _junit_xml([_plain_pair(i) for i in NON_TIMING[1::3] if i != lost])
    )
    assert_fails_with(fx.run(), "missing from every leg", lost)


def test_a_test_in_two_legs_fails_and_is_named(fx: Fixture) -> None:
    twice = NON_TIMING[0]
    fx.edit_ids(2, "unit_full", lambda ids: ids.append(twice))
    result = fx.run()
    assert_fails_with(result, "more than one leg", twice)


def test_a_test_repeated_inside_one_leg_fails(fx: Fixture) -> None:
    fx.edit_ids(2, "unit_full", lambda ids: ids.append(ids[0]))
    assert_fails_with(fx.run(), "leg 2", "unit_full", "more than once", NON_TIMING[1])


def test_an_unexpected_test_fails_and_is_named(fx: Fixture) -> None:
    ghost = "tests/test_ghost.py::test_never_collected"
    fx.edit_ids(3, "unit_full", lambda ids: ids.append(ghost))
    assert_fails_with(fx.run(), "not collected", ghost)


def test_a_timing_test_selected_by_unit_full_fails(fx: Fixture) -> None:
    fx.edit_ids(3, "unit_full", lambda ids: ids.append(TIMING[0]))
    result = fx.run()
    assert_fails_with(result, TIMING[0])


def test_timing_ids_different_from_the_collected_timing_set_fail(fx: Fixture) -> None:
    fx.edit_ids(1, "unit_timing", lambda ids: ids.pop())
    assert_fails_with(fx.run(), "timing", "differs", TIMING[1])


def test_an_empty_timing_set_fails(fx: Fixture) -> None:
    fx.collector.timing = []
    fx.edit_ids(1, "unit_timing", lambda ids: ids.clear())
    fx.write_junit(1, "unit_timing", _junit_xml([]))
    fx.collector.everything = list(NON_TIMING)
    assert_fails_with(fx.run(), "no timing tests")


# ---------------------------------------------------------------------------
# A6: what a leg executed is what it selected
# ---------------------------------------------------------------------------


def test_a_substituted_junit_identity_with_the_same_count_fails(fx: Fixture) -> None:
    substituted = NON_TIMING[0]  # selected by leg 1, written into leg 2's JUnit in place of one
    cases = [_plain_pair(i) for i in NON_TIMING[1::3]]
    cases[0] = _plain_pair(substituted)
    fx.write_junit(2, "unit_full", _junit_xml(cases))
    result = fx.run()
    assert_fails_with(
        result, "leg 2", "unit_full", "recorded but not selected", "tests.test_mod0::test_case[0]"
    )
    assert_fails_with(
        result, "leg 2", "unit_full", "selected but not recorded", "tests.test_mod0::test_case[1]"
    )


def test_a_junit_file_with_a_different_count_fails(fx: Fixture) -> None:
    fx.write_junit(3, "unit_full", _junit_xml([_plain_pair(i) for i in NON_TIMING[2::3]][:-1]))
    assert_fails_with(fx.run(), "leg 3", "unit_full", "selected but not recorded")


def test_a_junit_file_with_an_extra_case_fails(fx: Fixture) -> None:
    cases = [_plain_pair(i) for i in NON_TIMING[2::3]] + [("tests.test_extra", "test_x")]
    fx.write_junit(3, "unit_full", _junit_xml(cases))
    assert_fails_with(fx.run(), "leg 3", "unit_full", "tests.test_extra")


def test_a_junit_case_recorded_twice_fails(fx: Fixture) -> None:
    cases = [_plain_pair(i) for i in NON_TIMING[2::3]]
    fx.write_junit(3, "unit_full", _junit_xml([*cases, cases[0]]))
    assert fx.run().ok is False


@pytest.mark.parametrize("kind", ["failing", "erroring"])
def test_junit_failures_and_errors_fail(fx: Fixture, kind: str) -> None:
    cases = [_plain_pair(i) for i in NON_TIMING[0::3]]
    fx.write_junit(1, "unit_full", _junit_xml(cases, **{kind: [0]}))
    assert_fails_with(fx.run(), "leg 1", "unit_full", "failure or error")


def test_a_skipped_junit_case_is_accepted_and_counted(fx: Fixture) -> None:
    cases = [_plain_pair(i) for i in NON_TIMING[1::3]]
    fx.write_junit(2, "unit_full", _junit_xml(cases, skipped=[0, 2]))
    result = fx.run()
    assert result.ok is True
    assert result.report["shards"][1]["junit"]["skipped"] == 2
    assert result.report["shards"][1]["junit"]["tests"] == 3
    assert result.report["shards"][0]["junit"]["skipped"] == 0


def test_the_sums_over_the_legs_are_recorded(fx: Fixture) -> None:
    fx.write_junit(
        3, "unit_full", _junit_xml([_plain_pair(i) for i in NON_TIMING[2::3]], skipped=[1])
    )
    result = fx.run()
    junit = [s["junit"] for s in result.report["shards"]]
    assert sum(j["tests"] for j in junit) == 11
    assert sum(j["skipped"] for j in junit) == 1


COLLIDING = ["tests/test_a/test_b.py::test_x", "tests/test_a.test_b.py::test_x"]


def test_two_collected_ids_with_one_junit_identity_fail_and_both_are_named(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, non_timing=[*NON_TIMING[:7], *COLLIDING])
    result = fixture.run()
    assert_fails_with(result, COLLIDING[0], COLLIDING[1], "JUnit identity")


def test_a_collision_is_not_hidden_when_a_leg_records_one_in_place_of_the_other(
    tmp_path: Path,
) -> None:
    groups = [
        [NON_TIMING[0], NON_TIMING[3], COLLIDING[0]],
        [NON_TIMING[1], NON_TIMING[4], COLLIDING[1]],
        [NON_TIMING[2], NON_TIMING[5]],
    ]
    fixture = Fixture(tmp_path, non_timing=[i for g in groups for i in g], per_leg=groups)
    cases = [_plain_pair(i) for i in groups[1]]
    cases[2] = _plain_pair(COLLIDING[0])  # same identity as the id leg 2 selected
    fixture.write_junit(2, "unit_full", _junit_xml(cases))
    result = fixture.run()
    assert_fails_with(result, COLLIDING[0], COLLIDING[1], "JUnit identity")


# ---- missing, malformed or wrong-typed ids and JUnit files ----


def _remove(path: Path) -> None:
    path.unlink()


def _ids_not_json(path: Path) -> None:
    path.write_text("{oops", encoding="utf-8")


def _ids_list(path: Path) -> None:
    path.write_text('["tests/a.py::t"]', encoding="utf-8")


def _ids_without_list(path: Path) -> None:
    path.write_text('{"ids": "tests/a.py::t"}', encoding="utf-8")


def _ids_wrong_items(path: Path) -> None:
    path.write_text('{"ids": [1, 2]}', encoding="utf-8")


@pytest.mark.parametrize(
    "corrupt", [_remove, _ids_not_json, _ids_list, _ids_without_list, _ids_wrong_items]
)
def test_a_bad_ids_file_is_a_reason_that_names_the_file(
    fx: Fixture, corrupt: Callable[[Path], None]
) -> None:
    corrupt(fx.leg_dir(2) / "ids-unit_full.json")
    result = fx.run()
    assert_fails_with(result, "proof-shard-2/ids-unit_full.json")


def _xml_missing(path: Path) -> None:
    path.unlink()


def _xml_malformed(path: Path) -> None:
    path.write_text("<testsuites><testsuite>", encoding="utf-8")


def _xml_binary(path: Path) -> None:
    path.write_bytes(b"\xff\xfe\x00")


def _xml_entity_bomb(path: Path) -> None:
    path.write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<testsuites><testsuite><testcase classname="&b;" name="x"/></testsuite></testsuites>',
        encoding="utf-8",
    )


def _xml_wrong_root(path: Path) -> None:
    path.write_text("<nothing/>", encoding="utf-8")


def _xml_case_without_name(path: Path) -> None:
    path.write_text(
        '<testsuites><testsuite><testcase classname="a"/></testsuite></testsuites>',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("corrupt", "needle"),
    [
        (_xml_missing, "not readable JUnit XML"),
        (_xml_malformed, "not readable JUnit XML"),
        (_xml_binary, "not readable JUnit XML"),
        (_xml_entity_bomb, "not readable JUnit XML"),
        (_xml_wrong_root, "selected but not recorded"),
        (_xml_case_without_name, "no classname or name"),
    ],
)
def test_a_bad_junit_file_is_a_reason_that_names_the_leg_and_file(
    fx: Fixture, corrupt: Callable[[Path], None], needle: str
) -> None:
    corrupt(fx.leg_dir(3) / "junit-unit_full.xml")
    result = fx.run()
    assert_fails_with(
        result,
        "proof-shard-3/junit-unit_full.xml" if "XML" in needle or "name" in needle else "leg 3",
        needle,
    )


def test_a_failing_independent_collection_is_a_reason_and_nothing_is_raised(fx: Fixture) -> None:
    def broken(args: Sequence[str]) -> list[str]:
        raise CollectionError("pytest collection failed: exit 2")

    result = aggregate(
        fx.contract,
        reports_dir=fx.reports,
        leg_result="success",
        expected_sha=SHA,
        collector=broken,
    )
    assert_fails_with(result, "collection failed")


def test_an_unexpected_error_in_a_check_is_a_reason_not_an_exception(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.proof.aggregate as module

    def explode(*args: object, **kwargs: object) -> None:
        raise ValueError("kaboom")

    monkeypatch.setattr(module, "check_union", explode)
    result = fx.run()
    assert_fails_with(result, "internal error", "kaboom")


def test_the_default_collector_failure_surfaces_as_a_reason(tmp_path: Path) -> None:
    from scripts.proof.aggregate import subprocess_collector

    toy = tmp_path / "toy"
    (toy / "tests").mkdir(parents=True)
    (toy / "tests" / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    collect = subprocess_collector(toy)
    with pytest.raises(CollectionError, match="collection"):
        collect(["tests/", "-q"])


def test_the_default_collector_collects_a_toy_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.proof.aggregate import subprocess_collector

    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_CURRENT_TEST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing_matches_this")
    toy = tmp_path / "toy"
    (toy / "tests").mkdir(parents=True)
    (toy / "tests" / "test_ok.py").write_text(
        "def test_a():\n    pass\n\ndef test_b():\n    pass\n"
    )
    assert subprocess_collector(toy)(["tests/", "-q"]) == [
        "tests/test_ok.py::test_a",
        "tests/test_ok.py::test_b",
    ]


# ---- the JUnit identity rule against what real pytest writes ----

TOY_IDENTITY_SUITE = """
import pytest

class TestOuter:
    class TestInner:
        def test_nested(self):
            pass

    def test_method(self):
        pass

@pytest.mark.parametrize("value", ["a b", "x::y", "[z]", "p[q]r", "", "é"])
def test_ids(value):
    pass

@pytest.mark.parametrize("value", ["http://[::1]", "k=v;w", "tab\\there"])
def test_more(value):
    pass

def test_plain():
    pass
"""


def test_the_derived_junit_identity_equals_what_real_pytest_writes(tmp_path: Path) -> None:
    toy = tmp_path / "toy"
    (toy / "tests" / "pkg" / "sub").mkdir(parents=True)
    (toy / "tests" / "pkg" / "sub" / "test_deep.py").write_text(TOY_IDENTITY_SUITE)
    (toy / "tests" / "test_top.py").write_text(TOY_IDENTITY_SUITE)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    junit = tmp_path / "toy.xml"
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
        ],
        cwd=toy,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    ids_file = tmp_path / "ids.json"
    done = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "ci_shard_union.py"),
            "--emit-ids",
            str(ids_file),
            "--",
            "tests",
        ],
        cwd=toy,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    ids = json.loads(ids_file.read_text(encoding="utf-8"))["ids"]

    from defusedxml import ElementTree

    cases = [
        (c.get("classname"), c.get("name"))
        for c in ElementTree.parse(junit).getroot().iter("testcase")
    ]
    assert len(ids) == len(cases) > 20
    assert sorted(junit_pair(i) for i in ids) == sorted(cases)
    assert any("::" in name for _, name in cases)  # parameter ids holding `::` are covered
    assert any("[" in name.partition("[")[2] for _, name in cases)


@pytest.mark.parametrize("char", ["\x00", "\x07", "\x7f", "\ufffe", "\U0010ffff", "é", "ok"])
def test_junit_pair_escapes_characters_pytest_escapes_in_a_name(char: str) -> None:
    from _pytest.junitxml import bin_xml_escape

    node_id = f"tests/test_a.py::TestX::test_y[{char}]"
    assert junit_pair(node_id) == ("tests.test_a.TestX", bin_xml_escape(f"test_y[{char}]"))


END_TO_END_SUITE = """
import pytest

@pytest.mark.parametrize("value", [1, 2, 3, 4])
def test_param(value):
    assert value

def test_plain():
    assert True

class TestGroup:
    def test_method(self):
        assert True

@pytest.mark.timing
def test_timed():
    assert True
"""


def test_three_real_legs_and_the_real_collector_aggregate_to_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real runner, real pytest and JUnit, real collector: nothing on this path is stubbed."""
    from scripts.proof.aggregate import subprocess_collector

    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_CURRENT_TEST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "pytest").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m pytest "$@"\n')
    (shim / "pytest").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    root = tmp_path / "toy"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_toy.py").write_text(END_TO_END_SUITE, encoding="utf-8")
    payload = _payload()
    payload["gates"] = [
        g for g in payload["gates"] if g["id"] in {"format", "unit_full", "unit_timing"}
    ]
    payload["modes"]["full"]["phases"] = ["format", "unit_full", "unit_timing"]
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    contract = load_contract(contract_path)

    for leg in (1, 2, 3):
        report = ProofRunner(contract, root=root).run(
            mode="full", no_cache=True, otel=True, shard=leg
        )
        assert report.ok, [g.log_slice for g in report.gates if g.status == "fail"]
        body = report.to_dict()
        body["head_sha"] = SHA
        (root / f"proof-shard-{leg}" / "proof-report-full.json").write_text(
            json.dumps(body), encoding="utf-8"
        )

    result = aggregate(
        contract,
        reports_dir=root,
        leg_result="success",
        expected_sha=SHA,
        collector=subprocess_collector(root),
    )
    assert result.reasons == []
    assert result.ok is True
    assert sum(s["junit"]["tests"] for s in result.report["shards"]) == 7
    assert result.report["shards"][0]["junit"]["tests"] >= 1

    (root / "proof-shard-2" / "junit-unit_full.xml").write_text(
        _junit_xml([("tests.test_toy", "test_param[9]")]), encoding="utf-8"
    )
    broken = aggregate(
        contract,
        reports_dir=root,
        leg_result="success",
        expected_sha=SHA,
        collector=subprocess_collector(root),
    )
    assert broken.ok is False
    assert reasons_with(broken, "leg 2", "unit_full", "recorded but not selected")


# ---------------------------------------------------------------------------
# The merged report and the command line
# ---------------------------------------------------------------------------

TODAYS_KEYS = {
    "schema_version",
    "ok",
    "mode",
    "cache_mode",
    "runner",
    "contract_name",
    "head_sha",
    "gates",
    "failures",
    "otel",
}


def test_the_merged_report_keeps_todays_keys_and_adds_shards(fx: Fixture) -> None:
    report = fx.run().report
    assert set(report) == TODAYS_KEYS | {"shards"}
    assert report["schema_version"] == 1
    assert (report["mode"], report["cache_mode"], report["runner"]) == (
        "full",
        "no-cache",
        "native",
    )
    assert report["contract_name"] == "fixture-repo"
    assert report["head_sha"] == SHA
    assert [g["id"] for g in report["gates"]] == [
        "format",
        "unit_targeted",
        "unit_full",
        "unit_timing",
        "security_bandit",
        "promptfoo",
    ]
    for gate in report["gates"]:
        assert {"id", "phase", "classify", "command", "exit_code", "status", "duration_ms"} <= set(
            gate
        )
    assert report["failures"] == []


def test_the_merged_gate_entry_is_the_worst_status_with_the_longest_duration(fx: Fixture) -> None:
    fx.edit_gate(1, "unit_full", lambda g: g.update(duration_ms=100))
    fx.edit_gate(2, "unit_full", lambda g: g.update(duration_ms=900, status="fail", exit_code=1))
    fx.edit_gate(3, "unit_full", lambda g: g.update(duration_ms=300))
    merged = {g["id"]: g for g in fx.run().report["gates"]}
    assert merged["unit_full"]["status"] == "fail"
    assert merged["unit_full"]["duration_ms"] == 900
    assert merged["format"]["status"] == "pass"
    assert merged["unit_timing"]["status"] == "pass"  # ran in leg 1, skipped in legs 2 and 3
    assert merged["promptfoo"]["status"] == "skip"


def test_blocked_beats_pass_and_skip_only_when_every_leg_skipped(fx: Fixture) -> None:
    fx.edit_gate(3, "format", lambda g: g.update(status="blocked", exit_code=127))
    merged = {g["id"]: g for g in fx.run().report["gates"]}
    assert merged["format"]["status"] == "blocked"


def test_aggregate_reasons_are_failures_and_otel_spans_carry_the_leg(fx: Fixture) -> None:
    fx.edit_report(2, _set("head_sha", "d" * 40))
    report = fx.run().report
    reason_entries = [f for f in report["failures"] if f["gate"] == "shard_aggregate"]
    assert reason_entries
    assert all(
        f["classify"] == "test" and f["required"] is True and f["shard"] is None
        for f in reason_entries
    )
    spans = report["otel"]["spans"]
    assert {s["attributes"]["proof.shard"] for s in spans} == {1, 2, 3}
    assert report["otel"]["resource"]["service.name"] == "verdict-proof"


def test_reasons_are_bounded_in_size(fx: Fixture) -> None:
    ghosts = [f"tests/test_ghost.py::test_{i}" for i in range(500)]
    fx.edit_ids(3, "unit_full", lambda ids: ids.extend(ghosts))
    result = fx.run()
    assert result.ok is False
    assert all(len(f["log_slice"]) <= 4000 for f in result.report["failures"])
    assert reasons_with(result, "not collected", "more")


def _argv(fx: Fixture, out: Path, *extra: str, leg_result: str = "success") -> list[str]:
    contract_path = fx.tmp_path / "contract.yaml"
    return [
        "--contract",
        str(contract_path),
        "--reports",
        str(fx.reports),
        "--leg-result",
        leg_result,
        "--expected-sha",
        SHA,
        "--report",
        str(out),
        *extra,
    ]


def test_the_command_line_passes_and_writes_the_report(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    out = fx.tmp_path / "out" / "proof-report-full.json"
    assert main(_argv(fx, out, "--json"), collector=fx.collector) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_the_command_line_fails_and_still_writes_the_report(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(fx.leg_dir(2))
    out = fx.tmp_path / "proof-report-full.json"
    assert main(_argv(fx, out, leg_result="failure"), collector=fx.collector) == 1
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["ok"] is False
    assert any(f["gate"] == "shard_aggregate" for f in written["failures"])
    text = capsys.readouterr().out
    assert "FAIL" in text and "leg 2" in text


def test_the_command_line_fails_when_the_contract_cannot_be_read(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    out = fx.tmp_path / "proof-report-full.json"
    argv = _argv(fx, out)
    argv[argv.index("--contract") + 1] = str(fx.tmp_path / "missing.yaml")
    assert main(argv, collector=fx.collector) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is False


def test_the_command_line_ignores_its_own_previous_output(fx: Fixture) -> None:
    out = fx.reports / "proof-report-full.json"
    out.write_text(json.dumps({"stale": True}), encoding="utf-8")
    assert main(_argv(fx, out), collector=fx.collector) == 0


def test_a_contract_without_shards_fails_closed(fx: Fixture, tmp_path: Path) -> None:
    payload = _payload()
    del payload["modes"]["full"]["shards"]
    path = tmp_path / "noshards.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    result = aggregate(
        load_contract(path),
        reports_dir=fx.reports,
        leg_result="success",
        expected_sha=SHA,
        collector=fx.collector,
    )
    assert_fails_with(result, "declares no shards")


def test_a_missing_reports_directory_fails_closed(fx: Fixture) -> None:
    result = aggregate(
        fx.contract,
        reports_dir=fx.tmp_path / "nowhere",
        leg_result="success",
        expected_sha=SHA,
        collector=fx.collector,
    )
    assert_fails_with(result, "reports directory")
