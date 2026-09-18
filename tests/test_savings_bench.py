"""BOD-101 / BOD-114 paired legit-task savings bench: simulation never claims; live binds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict.savings_bench import (
    DEFAULT_SAVINGS_FIXTURE_PATH,
    MODE_LIVE_PAIRED,
    MODE_SIMULATION,
    TALK_TRACK,
    WITHHOLD_SIMULATION,
    format_savings_report,
    parse_measured_cost,
    run_savings_bench,
)
from verdict.savings_execution import (
    ArmExecution,
    ArmRequest,
    canonical_input_hash,
    input_hash_from_sent_payload,
    validate_quality,
)
from verdict.savings_live import (
    LiveExecutorUnavailableError,
    executor_from_env,
    omniroute_arm_executor,
)

FRONTIER = "cx/gpt-5.6-sol"


def test_parse_measured_cost_reads_omniroute_headers_only() -> None:
    cost = parse_measured_cost(
        {
            "headers": {
                "X-OmniRoute-Model": FRONTIER,
                "X-OmniRoute-Response-Cost": "0.012",
                "X-OmniRoute-Tokens-In": "100",
                "X-OmniRoute-Tokens-Out": "20",
                "X-OmniRoute-Cache-Hit": "false",
            }
        }
    )
    assert cost.usd == 0.012
    assert cost.tokens_in == 100
    assert cost.tokens_out == 20
    assert cost.cache_hit is False
    assert cost.source == "headers"


def test_parse_measured_cost_rejects_invented_fields() -> None:
    with pytest.raises(ValueError, match="X-OmniRoute-Response-Cost"):
        parse_measured_cost({"estimated_usd": 0.5, "headers": {}})


def test_arm_without_used_model_fails_closed(tmp_path: Path) -> None:
    fixture = json.loads(DEFAULT_SAVINGS_FIXTURE_PATH.read_text())
    del fixture["tasks"][0]["direct"]["headers"]["X-OmniRoute-Model"]
    path = tmp_path / "missing-model.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="must specify the model used"):
        run_savings_bench(path)


def test_contradictory_quality_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="contradictory"):
        validate_quality(True, ["missed the ADR"], where="verdict")
    fixture = json.loads(DEFAULT_SAVINGS_FIXTURE_PATH.read_text())
    fixture["tasks"][0]["verdict"]["quality"] = {"passed": True, "misses": ["missed the ADR"]}
    path = tmp_path / "contradictory.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="contradictory"):
        run_savings_bench(path)


# --- simulation (default fixture mode) ----------------------------------------


def test_simulation_is_labeled_and_never_claims() -> None:
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    assert report["talk_track"] == TALK_TRACK
    assert report["mode"] == MODE_SIMULATION
    assert report["executed"] is False
    assert report["claims_allowed"] is False
    assert report["evidence_bundle"] == []
    kinds = {task["kind"] for task in report["tasks"]}
    assert kinds == {"debug", "refactor_tests", "implement_from_ac"}
    for task in report["tasks"]:
        assert task["executed"] is False
        assert task["savings_claimed"] is False
        assert task["withhold_reason"] == WITHHOLD_SIMULATION
        assert WITHHOLD_SIMULATION in task["withhold_reasons"]
        assert task["direct"]["measured_from"] == "fixture (not executed)"
        assert task["verdict"]["measured_from"] == "fixture (not executed)"
        assert task["identity_binding"]["bound"] is False
        assert task["input_hash"].startswith("sha256:")
    assert report["aggregate"]["savings_claimed_count"] == 0
    assert report["aggregate"]["executed_count"] == 0
    assert report["aggregate"]["simulation_count"] == 4
    assert report["aggregate"]["claimed_cost_delta_usd"] == 0
    assert report["aggregate"]["measured_cost_delta_usd"] == 0
    assert report["aggregate"]["stated_cost_delta_usd"] < 0
    rendered = format_savings_report(report)
    assert "mode: simulation-not-executed" in rendered
    assert "claims_allowed: false" in rendered
    assert "savings_claimed: 0" in rendered
    assert "NOTE: simulation" in rendered


def test_simulation_still_labels_quality_miss_and_cache_hit_as_secondary_reasons() -> None:
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    by_id = {task["task_id"]: task for task in report["tasks"]}
    miss = by_id["implement-from-ac"]
    assert miss["verdict"]["quality"]["passed"] is False
    assert "quality_miss" in miss["withhold_reasons"]
    replay = by_id["debug-null-deref-cache-replay"]
    assert replay["verdict"]["cache_hit"] is True
    assert "cache_hit_is_not_model_savings" in replay["withhold_reasons"]
    assert report["aggregate"]["quality_miss_count"] == 1
    assert report["aggregate"]["cache_hit_count"] == 1
    rendered = format_savings_report(report)
    assert "cache_hits: 1" in rendered
    assert "cache_hit=true" in rendered
    assert "quality_miss" in rendered


def test_simulation_explains_fixture_identity_vs_live_route() -> None:
    fixture = json.loads(DEFAULT_SAVINGS_FIXTURE_PATH.read_text())
    stated = {
        task["id"]: task["verdict"]["headers"]["X-OmniRoute-Model"] for task in fixture["tasks"]
    }
    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH)
    assert "claude-3-opus" not in json.dumps(report)
    for task in report["tasks"]:
        binding = task["identity_binding"]
        assert binding["completed_with"] == stated[task["task_id"]]
        assert binding["routed"] == task["verdict"]["routed"]
        assert "not bound to any execution" in binding["explanation"]
        if binding["completed_with"] != binding["routed"]:
            assert "live chooser routed" in binding["explanation"]
        assert "opus" not in task["verdict"]["routed"]


def test_cmd_benchmark_savings_default_is_simulation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli

    output = tmp_path / "savings.json"
    cli.cmd_benchmark("benchmarks/fixtures/reproducible.json", str(output), savings=True)
    out = capsys.readouterr().out
    assert "talk_track: we measure" in out
    assert "mode: simulation-not-executed" in out
    assert "savings_claimed: 0" in out
    assert "simulation_not_executed" in out
    payload = json.loads(output.read_text())
    assert payload["mode"] == MODE_SIMULATION
    assert payload["claims_allowed"] is False
    assert payload["aggregate"]["task_count"] == 4
    assert payload["aggregate"]["savings_claimed_count"] == 0


def test_cmd_benchmark_live_paired_refuses_without_gateway(monkeypatch, tmp_path: Path) -> None:
    from verdict import cli

    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_benchmark(
            "benchmarks/fixtures/reproducible.json",
            str(tmp_path / "x.json"),
            savings=True,
            live_paired=True,
        )
    assert exc.value.code == 2
    assert not (tmp_path / "x.json").exists()
    with pytest.raises(LiveExecutorUnavailableError):
        executor_from_env({})


# --- live paired mode via the execute_arm hook --------------------------------


def _fake_executor(
    *,
    costs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
    completed: dict[str, str] | None = None,
    execution_ids: dict[str, str] | None = None,
    input_hash_override: dict[str, str] | None = None,
    cache_hit: dict[str, str] | None = None,
    record: list[ArmRequest] | None = None,
):
    costs = costs or {"direct": "0.0200", "verdict": "0.0010"}
    outputs = outputs or {
        "direct": "The null path is in the receipt serializer; per the ADR, control plane test receipt.",
        "verdict": "Null path found in the ADR-governed receipt serializer; control plane test receipt.",
    }

    def execute(request: ArmRequest) -> ArmExecution:
        if record is not None:
            record.append(request)
        used = (completed or {}).get(request.arm, request.model)
        headers = {
            "x-omniroute-request-id": (execution_ids or {}).get(request.arm, f"run-{request.arm}"),
            "x-omniroute-model": used,
            "x-omniroute-response-cost": costs[request.arm],
            "x-omniroute-tokens-in": "900",
            "x-omniroute-tokens-out": "120",
            "x-omniroute-cache-hit": (cache_hit or {}).get(request.arm, "false"),
        }
        return ArmExecution(
            arm=request.arm,
            execution_id=headers["x-omniroute-request-id"],
            input_hash=(input_hash_override or {}).get(request.arm, request.input_hash),
            completed_with=used,
            gateway="omniroute",
            output=outputs[request.arm],
            headers=headers,
            receipt={"status_code": 200},
            attempt_chain=request.attempt_chain,
            status_code=200,
        )

    return execute


def test_live_paired_claims_only_with_complete_bound_evidence() -> None:
    seen: list[ArmRequest] = []
    report = run_savings_bench(
        DEFAULT_SAVINGS_FIXTURE_PATH, execute_arm=_fake_executor(record=seen)
    )
    assert report["mode"] == MODE_LIVE_PAIRED
    assert report["claims_allowed"] is True
    assert report["aggregate"]["executed_count"] == 4
    assert len(seen) == 8

    # Both arms received the same input hash and acceptance criteria per task.
    by_task: dict[str, list[ArmRequest]] = {}
    for request in seen:
        by_task.setdefault(request.task_id, []).append(request)
    for task_id, requests in by_task.items():
        assert {r.arm for r in requests} == {"direct", "verdict"}, task_id
        assert len({r.input_hash for r in requests}) == 1
        assert len({r.acceptance_criteria for r in requests}) == 1
        verdict_req = next(r for r in requests if r.arm == "verdict")
        assert verdict_req.context_envelope, "verdict arm executes the hydrated pack"
        assert verdict_req.routed_model == verdict_req.model
        assert verdict_req.model in verdict_req.attempt_chain

    by_id = {task["task_id"]: task for task in report["tasks"]}
    debug = by_id["debug-null-deref"]
    assert debug["executed"] is True
    assert debug["savings_claimed"] is True
    assert debug["withhold_reasons"] == []
    assert debug["direct"]["execution_id"] == "run-direct"
    assert debug["verdict"]["execution_id"] == "run-verdict"
    assert debug["direct"]["cost_source"] == "headers"
    assert debug["verdict"]["completed_with"] == debug["verdict"]["routed"]
    assert debug["identity_binding"]["bound"] is True
    assert debug["verdict"]["quality"]["evaluator"] == "checks.must_contain"
    assert debug["verdict"]["pack_state"] == "hydrated"
    assert debug["deltas"]["cost_usd"] < 0
    assert report["aggregate"]["savings_claimed_count"] >= 1
    assert report["aggregate"]["measured_cost_delta_usd"] < 0
    assert report["aggregate"]["stated_cost_delta_usd"] == 0

    # Evidence bundle is privacy-reviewed: digests and gateway headers, never text.
    assert len(report["evidence_bundle"]) == 8
    encoded = json.dumps(report["evidence_bundle"])
    assert "Null path found" not in encoded
    for row in report["evidence_bundle"]:
        assert row["execution_id"]
        assert row["input_hash"].startswith("sha256:")
        assert row["output_digest"].startswith("sha256:")
        assert set(row["headers"]) <= {
            "x-omniroute-request-id",
            "x-omniroute-model",
            "x-omniroute-response-cost",
            "x-omniroute-tokens-in",
            "x-omniroute-tokens-out",
            "x-omniroute-cache-hit",
        }
    rendered = format_savings_report(report)
    assert "mode: live-paired" in rendered
    assert "NOTE: simulation" not in rendered


def test_live_quality_is_evaluated_from_output_not_fixture() -> None:
    report = run_savings_bench(
        DEFAULT_SAVINGS_FIXTURE_PATH,
        execute_arm=_fake_executor(
            outputs={"direct": "fine answer about null and adr", "verdict": "unrelated chatter"}
        ),
    )
    for task in report["tasks"]:
        assert task["savings_claimed"] is False
        assert "quality_miss" in task["withhold_reasons"]
        assert task["verdict"]["quality"]["passed"] is False
        assert task["verdict"]["quality"]["misses"]


def test_live_refuses_when_execution_evidence_is_missing_or_unbound() -> None:
    cases = {
        "missing_execution_id": _fake_executor(execution_ids={"verdict": ""}),
        "input_hash_mismatch": _fake_executor(input_hash_override={"direct": "sha256:" + "0" * 64}),
        "identity_unbound": _fake_executor(completed={"verdict": ""}),
        "completed_identity_not_in_attempt_chain": _fake_executor(
            completed={"verdict": "some/other-model"}
        ),
        "cache_hit_is_not_model_savings": _fake_executor(cache_hit={"verdict": "true"}),
        "no_cost_reduction": _fake_executor(costs={"direct": "0.0010", "verdict": "0.0200"}),
    }
    for expected, executor in cases.items():
        report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH, execute_arm=executor)
        assert report["aggregate"]["savings_claimed_count"] == 0, expected
        for task in report["tasks"]:
            assert task["savings_claimed"] is False, expected
            assert any(expected in reason for reason in task["withhold_reasons"]), (
                expected,
                task["withhold_reasons"],
            )
    unbound = run_savings_bench(
        DEFAULT_SAVINGS_FIXTURE_PATH, execute_arm=cases["completed_identity_not_in_attempt_chain"]
    )
    assert all(t["identity_binding"]["bound"] is False for t in unbound["tasks"])
    assert all(
        "not in the attempt chain" in t["identity_binding"]["explanation"] for t in unbound["tasks"]
    )


def test_live_executor_exceptions_become_evidence_gaps_not_crashes() -> None:
    def boom(request: ArmRequest) -> ArmExecution:
        if request.arm == "verdict":
            raise httpx.ConnectError("gateway down")
        return _fake_executor()(request)

    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH, execute_arm=boom)
    for task in report["tasks"]:
        assert task["executed"] is False
        assert task["savings_claimed"] is False
        assert "verdict:not_executed" in task["withhold_reasons"]
        assert "ConnectError" in task["executor_errors"]["verdict"]
        assert task["verdict"]["measured_from"] == "not executed"


def test_fallback_identity_within_attempt_chain_is_explained_and_bound() -> None:
    def fallback(request: ArmRequest) -> ArmExecution:
        if request.arm == "verdict" and len(request.attempt_chain) > 1:
            return _fake_executor(completed={"verdict": request.attempt_chain[1]})(request)
        return _fake_executor()(request)

    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH, execute_arm=fallback)
    explained = [
        t
        for t in report["tasks"]
        if t["identity_binding"]["completed_with"] != t["verdict"]["routed"]
    ]
    assert explained
    for task in explained:
        assert task["identity_binding"]["bound"] is True
        assert (
            "fell back within the observed attempt chain" in task["identity_binding"]["explanation"]
        )
        assert "completed_identity_not_in_attempt_chain" not in task["withhold_reasons"]


# --- OmniRoute-backed executor (mock transport; no network) -------------------


def test_omniroute_arm_executor_returns_raw_gateway_evidence() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={
                "X-OmniRoute-Request-Id": "req-123",
                "X-OmniRoute-Model": "opencode/hy3-free",
                "X-OmniRoute-Response-Cost": "0.0011",
                "X-OmniRoute-Tokens-In": "700",
                "X-OmniRoute-Tokens-Out": "90",
                "X-OmniRoute-Cache-Hit": "false",
            },
            json={
                "id": "chatcmpl-1",
                "model": "hy3-free",
                "choices": [{"message": {"role": "assistant", "content": "null path in adr"}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 90},
            },
        )

    execute = omniroute_arm_executor(
        "http://127.0.0.1:20128/v1", "secret", transport=httpx.MockTransport(handler)
    )
    request = ArmRequest(
        arm="verdict",
        task_id="t",
        prompt="Debug the null dereference. Cite the ADR.",
        acceptance_criteria=("names the null path",),
        input_hash=canonical_input_hash(
            "Debug the null dereference. Cite the ADR.", ["names the null path"]
        ),
        model="opencode/hy3-free",
        context_envelope="[INSTRUCTIONS:task]\nhydrated pack\n",
        routed_model="opencode/hy3-free",
        attempt_chain=("opencode/hy3-free",),
    )
    execution = execute(request)
    assert captured["url"] == "http://127.0.0.1:20128/v1/chat/completions"
    assert captured["auth"] == "Bearer secret"
    assert captured["body"]["model"] == "opencode/hy3-free"
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": "[INSTRUCTIONS:task]\nhydrated pack\n",
    }
    assert "Acceptance criteria:" in captured["body"]["messages"][1]["content"]
    assert execution.execution_id == "req-123"
    assert execution.completed_with == "opencode/hy3-free"
    assert execution.input_hash == request.input_hash
    assert execution.output == "null path in adr"
    assert execution.status_code == 200
    cost = parse_measured_cost({"headers": dict(execution.headers)})
    assert cost.usd == 0.0011 and cost.source == "headers"
    assert execution.receipt["identity_source"] == "header"
    assert "authorization" not in execution.to_evidence()["headers"]


def test_omniroute_arm_executor_without_headers_yields_unbound_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    execute = omniroute_arm_executor(
        "http://127.0.0.1:20128", transport=httpx.MockTransport(handler)
    )
    request = ArmRequest(
        arm="direct",
        task_id="t",
        prompt="p",
        acceptance_criteria=("a",),
        input_hash=canonical_input_hash("p", ["a"]),
        model=FRONTIER,
    )
    execution = execute(request)
    assert execution.execution_id == ""
    assert execution.completed_with == ""
    with pytest.raises(ValueError):
        parse_measured_cost({"headers": dict(execution.headers)})


# --- BOD-116: evidence must be observed from the gateway, never copied --------


def _arm_request(arm: str = "verdict", prompt: str = "Debug the null dereference.") -> ArmRequest:
    criteria = ("names the null path",)
    return ArmRequest(
        arm=arm,
        task_id="t",
        prompt=prompt,
        acceptance_criteria=criteria,
        input_hash=canonical_input_hash(prompt, criteria),
        model="opencode/hy3-free",
        routed_model="opencode/hy3-free" if arm == "verdict" else None,
        attempt_chain=("opencode/hy3-free", "opencode/fallback-free") if arm == "verdict" else (),
    )


def test_omniroute_executor_ignores_body_model_without_identity_header() -> None:
    """An echoed request alias in the body is not provider-bound identity."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-OmniRoute-Request-Id": "req-1"},
            json={"model": "auto/cheap", "choices": [{"message": {"content": "x"}}]},
        )

    execute = omniroute_arm_executor(
        "http://127.0.0.1:20128", transport=httpx.MockTransport(handler)
    )
    execution = execute(_arm_request())
    assert execution.completed_with == ""
    assert execution.receipt["identity_source"] == "none"


def test_omniroute_executor_input_hash_is_derived_from_sent_bytes() -> None:
    """The execution's input hash is recomputed from the bytes that left the client."""
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(
            200,
            headers={"X-OmniRoute-Request-Id": "req-1", "X-OmniRoute-Model": "opencode/hy3-free"},
            json={"choices": [{"message": {"content": "x"}}]},
        )

    execute = omniroute_arm_executor(
        "http://127.0.0.1:20128", transport=httpx.MockTransport(handler)
    )

    honest = execute(_arm_request())
    assert honest.input_hash == input_hash_from_sent_payload(sent[-1])
    assert honest.input_hash == _arm_request().input_hash
    assert honest.receipt["input_hash_source"] == "sent_payload"
    assert honest.receipt["sent_payload_digest"].startswith("sha256:")

    # A request whose declared hash does not describe the prompt actually sent
    # must be caught: the executor cannot simply echo request.input_hash.
    stale = _arm_request()
    stale = ArmRequest(
        **{**stale.__dict__, "input_hash": canonical_input_hash("other prompt", ("a",))}
    )
    mismatched = execute(stale)
    assert mismatched.input_hash != stale.input_hash
    assert mismatched.input_hash == input_hash_from_sent_payload(sent[-1])


def test_input_hash_from_sent_payload_rejects_unparseable_bodies() -> None:
    assert input_hash_from_sent_payload(b"not json") == ""
    assert input_hash_from_sent_payload(b'{"messages": []}') == ""


def test_omniroute_executor_attempt_chain_is_observed_not_planned() -> None:
    chain_header: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "X-OmniRoute-Request-Id": "req-1",
                "X-OmniRoute-Model": "opencode/fallback-free",
                **chain_header,
            },
            json={"choices": [{"message": {"content": "x"}}]},
        )

    execute = omniroute_arm_executor(
        "http://127.0.0.1:20128", transport=httpx.MockTransport(handler)
    )

    unobserved = execute(_arm_request())
    assert unobserved.attempt_chain == (), "planned chain must not be reported as observed"

    chain_header["X-OmniRoute-Attempt-Chain"] = "opencode/hy3-free, opencode/fallback-free"
    observed = execute(_arm_request())
    assert observed.attempt_chain == ("opencode/hy3-free", "opencode/fallback-free")


def test_planned_fallback_without_observed_chain_is_refused() -> None:
    """A completed model that differs from routed needs gateway chain evidence, not a plan."""

    def fallback_without_receipt(request: ArmRequest) -> ArmExecution:
        if request.arm == "verdict" and len(request.attempt_chain) > 1:
            execution = _fake_executor(completed={"verdict": request.attempt_chain[1]})(request)
            return ArmExecution(**{**execution.__dict__, "attempt_chain": ()})
        return _fake_executor()(request)

    report = run_savings_bench(DEFAULT_SAVINGS_FIXTURE_PATH, execute_arm=fallback_without_receipt)
    fell_back = [
        t
        for t in report["tasks"]
        if t["identity_binding"]["completed_with"] != t["verdict"]["routed"]
    ]
    assert fell_back
    for task in fell_back:
        assert task["savings_claimed"] is False
        assert "verdict:completed_identity_not_in_attempt_chain" in task["withhold_reasons"]
        assert task["identity_binding"]["bound"] is False
        assert task["identity_binding"]["attempt_chain"] == []


def test_live_refuses_when_baseline_output_fails_checks() -> None:
    """A cheaper Verdict answer is not savings if the frontier baseline failed the task."""
    report = run_savings_bench(
        DEFAULT_SAVINGS_FIXTURE_PATH,
        execute_arm=_fake_executor(
            outputs={
                "direct": "unrelated chatter",
                "verdict": "Null path found in the ADR-governed receipt serializer; control plane test receipt.",
            }
        ),
    )
    for task in report["tasks"]:
        assert task["direct"]["quality"]["passed"] is False
        assert task["savings_claimed"] is False
        assert "baseline_quality_miss" in task["withhold_reasons"]
        assert "quality_miss" not in task["withhold_reasons"], "verdict arm quality is separate"
    assert report["aggregate"]["savings_claimed_count"] == 0
