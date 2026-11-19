"""Unit tests for the paired live usage bench (offline: stub transport, stub router)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

TASKS_JSON = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "fixtures"
    / "live_savings"
    / "tasks.json"
)
FIXTURE_DIR = TASKS_JSON.parent
REPO_ROOT = Path(__file__).resolve().parent.parent


class TestOptIn:
    """The benchmark must refuse to run without the opt-in env var."""

    def test_refuses_without_opt_in(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("VERDICT_LIVE_SMOKE", raising=False)
        from scripts.live_savings_bench import _refuse_without_opt_in

        with pytest.raises(SystemExit):
            _refuse_without_opt_in()


class TestTaskFixtures:
    """Validate the tasks.json fixture file itself."""

    def test_tasks_json_loads(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        assert "tasks" in data
        assert len(data["tasks"]) >= 10

    def test_each_task_has_required_fields(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        for task in data["tasks"]:
            assert "id" in task
            assert "task_class" in task
            assert "prompt" in task
            assert "test_file" in task
            assert "task_group" in task

    def test_each_task_test_file_exists(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        for task in data["tasks"]:
            test_path = FIXTURE_DIR / task["test_file"]
            assert test_path.exists(), f"{task['test_file']} missing"

    def test_task_ids_unique(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        ids = [t["id"] for t in data["tasks"]]
        assert len(ids) == len(set(ids))

    def test_at_least_5_repo_context_tasks(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        rc = [t for t in data["tasks"] if t["task_group"] == "repo-context"]
        assert len(rc) >= 5


FIZZBUZZ_OK = (
    "def fizzbuzz(n: int) -> list[str]:\n"
    "    out = []\n"
    "    for i in range(1, n + 1):\n"
    "        if i % 15 == 0:\n"
    "            out.append('FizzBuzz')\n"
    "        elif i % 3 == 0:\n"
    "            out.append('Fizz')\n"
    "        elif i % 5 == 0:\n"
    "            out.append('Buzz')\n"
    "        else:\n"
    "            out.append(str(i))\n"
    "    return out\n"
)


class TestGrading:
    """Acceptance is by running the task's own test file."""

    def test_correct_solution_passes(self) -> None:
        from scripts.live_savings_bench import _grade_solution

        assert _grade_solution({"id": "fizzbuzz", "test_file": "test_fizzbuzz.py"}, FIZZBUZZ_OK)

    def test_wrong_solution_fails(self) -> None:
        from scripts.live_savings_bench import _grade_solution

        code = "def fizzbuzz(n: int) -> list[str]:\n    return [str(i) for i in range(1, n + 1)]\n"
        assert not _grade_solution({"id": "fizzbuzz", "test_file": "test_fizzbuzz.py"}, code)


class TestCodeExtraction:
    def test_markdown_fence(self) -> None:
        from scripts.live_savings_bench import _extract_code

        assert _extract_code("Here:\n```python\ndef f(): pass\n```\n").strip() == "def f(): pass"

    def test_raw_code_passthrough(self) -> None:
        from scripts.live_savings_bench import _extract_code

        assert _extract_code("def f(): pass") == "def f(): pass"


# ── Stubs ──────────────────────────────────────────────────────────────


def _body(
    model: str, *, prompt: int = 100, cached: int = 0, completion: int = 50
) -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"content": "```python\nx = 1\n```"}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


class StubTransport:
    """Answers per requested model; records every request it was sent."""

    def __init__(
        self,
        replies: Mapping[str, list[tuple[int, dict[str, str], dict[str, Any]]]] | None = None,
        *,
        served: Mapping[str, str] | None = None,
        usage: Mapping[str, dict[str, int]] | None = None,
    ) -> None:
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.served = dict(served or {})
        self.usage = dict(usage or {})
        self.sent: list[tuple[str, list[dict[str, str]]]] = []

    def __call__(
        self, model: str, messages: list[dict[str, str]], timeout: float
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        self.sent.append((model, messages))
        queue = self.replies.get(model)
        if queue:
            return queue.pop(0)
        served = self.served.get(model, model)
        headers = {"X-OmniRoute-Model": served, "X-OmniRoute-Request-Id": f"req-{len(self.sent)}"}
        return 200, headers, _body(served, **self.usage.get(model, {}))


class StubRouter:
    """Stands in for the live IntelligenceService + EligibilityGate."""

    def __init__(
        self,
        *,
        model: str | None = "free/coder",
        decision: str = "selected",
        capacity: str = "free",
        pack: str | None = "PACK",
        probe_calls: int = 0,
        capacities: Mapping[str, str] | None = None,
    ) -> None:
        self.model = model
        self.decision = decision
        self.capacity = capacity
        self.pack = pack
        self.probe_calls = probe_calls
        self.capacities = dict(capacities or {})
        self.routed: list[str] = []

    def route(self, prompt: str, criticality: str) -> Any:
        from scripts.live_savings_bench import Call, RouteResult

        self.routed.append(prompt)
        probes = [
            Call(
                arm="verdict",
                kind="probe",
                requested_model=self.model or "",
                served_model=self.model or "",
                request_id=None,
                capacity=self.capacity,
                status=200,
                input_tokens=7,
                cached_input_tokens=0,
                output_tokens=1,
            )
            for _ in range(self.probe_calls)
        ]
        return RouteResult(
            model=self.model,
            decision=self.decision,
            reason="stub",
            context_pack_prompt=self.pack,
            capacity=self.capacity,
            probe_calls=probes,
            receipt={},
        )

    def capacity_of(self, model: str) -> str:
        return self.capacities.get(model, "subscription")


TASK = {"id": "t1", "task_group": "standalone", "task_class": "simple-code", "prompt": "P"}
TASK2 = {"id": "t2", "task_group": "repo-context", "task_class": "medium-code", "prompt": "Q"}


def _always(_task: Mapping[str, Any], _code: str) -> bool:
    return True


def _run(
    router: Any, transport: Any, *, tasks: list[dict[str, Any]] | None = None, grader: Any = None
) -> list[Any]:
    from scripts.live_savings_bench import run_pairs

    return run_pairs(
        tasks or [TASK],
        router=router,
        transport=transport,
        baseline_model="frontier/big",
        repeats=1,
        grader=grader or _always,
        max_wall_seconds=60.0,
    ).runs


def _report(runs: list[Any], **kw: Any) -> dict[str, Any]:
    from scripts.live_savings_bench import build_report

    return build_report(runs, config={"baseline_model": "frontier/big"}, **kw)


# ── Rule 1: Verdict arm on the live ladder, no pool, no fallback ───────


class TestVerdictArm:
    def test_no_hardcoded_candidate_pool(self) -> None:
        """Catches: a fixed VERDICT_CANDIDATES list deciding the Verdict arm."""
        import scripts.live_savings_bench as bench

        assert not hasattr(bench, "VERDICT_CANDIDATES")
        assert not hasattr(bench, "BASELINE_MODEL")

    def test_deny_is_verdict_miss_and_baseline_unaffected(self) -> None:
        """Catches: a deny silently falling back to the baseline model."""
        transport = StubTransport()
        runs = _run(StubRouter(model="no_eligible_target", decision="denied"), transport)
        verdict = next(r for r in runs if r.arm == "verdict")
        baseline = next(r for r in runs if r.arm == "baseline")
        assert verdict.miss_reason and "denied" in verdict.miss_reason
        assert not verdict.accepted
        assert verdict.calls == []
        assert baseline.accepted
        assert [m for m, _ in transport.sent] == ["frontier/big"]

    @pytest.mark.parametrize("route", ["auto/best-coding", "combo/free-first"])
    def test_opaque_route_is_a_miss(self, route: str) -> None:
        """Catches: an opaque alias standing in for a concrete Verdict route."""
        transport = StubTransport()
        runs = _run(StubRouter(model=route), transport)
        verdict = next(r for r in runs if r.arm == "verdict")
        assert verdict.miss_reason and "opaque" in verdict.miss_reason
        assert route not in [m for m, _ in transport.sent]

    def test_metered_capacity_is_a_miss(self) -> None:
        """Catches: the Verdict arm spending pay-per-token capacity."""
        transport = StubTransport()
        runs = _run(StubRouter(model="paid/model", capacity="metered"), transport)
        verdict = next(r for r in runs if r.arm == "verdict")
        assert verdict.miss_reason and "capacity" in verdict.miss_reason
        assert "paid/model" not in [m for m, _ in transport.sent]

    def test_verdict_sends_pack_plus_prompt_baseline_sends_prompt_only(self) -> None:
        """Catches: both arms sending the same input (no context-first treatment)."""
        transport = StubTransport()
        _run(StubRouter(pack="THE-PACK"), transport)
        sent = dict(transport.sent)
        verdict_text = json.dumps(sent["free/coder"])
        baseline_text = json.dumps(sent["frontier/big"])
        assert "THE-PACK" in verdict_text and "P" in verdict_text
        assert "THE-PACK" not in baseline_text

    def test_live_router_attaches_eligibility_gate_like_serve(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Catches: a Verdict arm built without the EligibilityGate (catalog-only path)."""
        import scripts.live_savings_bench as bench
        from verdict import api

        class Svc:
            eligibility_gate = None

            def _resolve_confirm_transport(self, endpoint: Any) -> Any:
                return None

        gate = object()
        monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
        monkeypatch.setattr(api, "_build_intelligence", lambda: Svc())
        monkeypatch.setattr(api, "_build_availability_cache", lambda: (object(), gate))
        router = bench.LiveRouter()
        assert router.service.eligibility_gate is gate

    def test_live_router_refuses_without_gate(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Catches: silently routing without the live ladder when OmniRoute is unset."""
        import scripts.live_savings_bench as bench
        from verdict import api

        monkeypatch.setattr(api, "_build_intelligence", lambda: object())
        monkeypatch.setattr(api, "_build_availability_cache", lambda: (object(), object()))
        monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
        with pytest.raises(SystemExit):
            bench.LiveRouter()
        monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
        monkeypatch.setattr(api, "_build_availability_cache", lambda: None)
        with pytest.raises(SystemExit):
            bench.LiveRouter()


# ── Rule 2: baseline flag, no default ──────────────────────────────────


class TestBaselineFlag:
    def test_baseline_model_is_required(self, tmp_path: Path) -> None:
        """Catches: a hard-coded default baseline model."""
        from scripts.live_savings_bench import parse_args

        with pytest.raises(SystemExit):
            parse_args(["--out-dir", str(tmp_path)])
        args = parse_args(["--out-dir", str(tmp_path), "--baseline-model", "x/y"])
        assert args.baseline_model == "x/y"
        assert args.repeats == 1

    def test_opaque_baseline_refused(self, tmp_path: Path) -> None:
        from scripts.live_savings_bench import validate_args

        args_ns = _ns(tmp_path, baseline_model="combo/x")
        with pytest.raises(SystemExit):
            validate_args(args_ns)


def _ns(out: Path, **kw: Any) -> Any:
    from scripts.live_savings_bench import parse_args

    argv = ["--out-dir", str(out), "--baseline-model", kw.pop("baseline_model", "x/y")]
    return parse_args(argv)


# ── Rule 3: per-call evidence and attribution ──────────────────────────


class TestCallEvidence:
    def test_served_model_request_id_cached_and_capacity_recorded(self) -> None:
        """Catches: losing the served model, request id or cached tokens of a call."""
        transport = StubTransport(
            served={"frontier/big": "frontier/big-served"},
            usage={"frontier/big": {"prompt": 300, "cached": 120, "completion": 40}},
        )
        runs = _run(StubRouter(capacities={"frontier/big": "subscription"}), transport)
        call = next(r for r in runs if r.arm == "baseline").calls[0]
        assert call.served_model == "frontier/big-served"
        assert call.request_id and call.request_id.startswith("req-")
        assert (call.input_tokens, call.cached_input_tokens, call.output_tokens) == (300, 120, 40)
        assert call.capacity == "subscription"

    def test_probe_and_retry_calls_count_against_their_arm(self) -> None:
        """Catches: routing probes and retries left out of the arm's token total."""
        transport = StubTransport(replies={"free/coder": [(503, {}, {})]})
        runs = _run(StubRouter(probe_calls=2), transport)
        verdict = next(r for r in runs if r.arm == "verdict")
        kinds = [c.kind for c in verdict.calls]
        assert kinds.count("probe") == 2
        assert "retry" in kinds
        assert all(c.arm == "verdict" for c in verdict.calls)
        report = _report(runs)
        tokens = report["arms"]["verdict"]["tokens"]["total"]
        assert tokens["input"] >= 14  # the two probes are counted


# ── Rules 4-8: report ──────────────────────────────────────────────────


class TestReport:
    def test_per_arm_per_group_accept_rate_and_tokens_by_capacity(self) -> None:
        """Catches: a report without groups, accept rates or the capacity split."""
        runs = _run(StubRouter(), StubTransport(), tasks=[TASK, TASK2])
        report = _report(runs)
        for arm in ("baseline", "verdict"):
            assert report["arms"][arm]["accepted"] == 2
            assert report["arms"][arm]["accepted_rate"] == 1.0
            assert set(report["groups"]) == {"standalone", "repo-context"}
            assert report["groups"]["repo-context"][arm]["attempted"] == 1
        by_cap = report["arms"]["verdict"]["tokens"]["by_capacity"]
        assert set(by_cap["free"]) == {"input", "cached_input", "output"}
        assert "subscription" in report["arms"]["baseline"]["tokens"]["by_capacity"]
        assert report["headline_metric"] == "tokens"

    def test_same_model_in_both_arms_reports_no_saving(self) -> None:
        """Catches: the 2026-09-28 failure, a 'saving' when both arms ran one model."""
        transport = StubTransport(
            served={"free/coder": "same/model", "frontier/big": "same/model"},
            usage={"free/coder": {"prompt": 10}, "frontier/big": {"prompt": 900}},
        )
        report = _report(_run(StubRouter(), transport))
        pair = report["pairs"][0]
        assert pair["status"] == "same_model"
        assert pair["token_delta"] is None
        assert report["savings"]["comparable_pairs"] == 0
        assert report["savings"]["claim_allowed"] is False

    def test_verdict_accepts_fewer_means_claim_not_allowed(self) -> None:
        """Catches: claiming a saving while Verdict accepts less work."""

        # Verdict is denied on t2, so it accepts 1 task against the baseline's 2.
        class SplitRouter(StubRouter):
            def route(self, prompt: str, criticality: str) -> Any:
                result = super().route(prompt, criticality)
                if prompt.startswith("Q"):
                    result.model, result.decision = "no_eligible_target", "denied"
                return result

        runs = _run(SplitRouter(), StubTransport(), tasks=[TASK, TASK2], grader=_always)
        report = _report(runs)
        assert report["arms"]["verdict"]["accepted"] < report["arms"]["baseline"]["accepted"]
        assert report["savings"]["claim_allowed"] is False
        assert any("fewer" in r for r in report["savings"]["reasons"])

    def test_failed_pair_never_counted_as_saving(self) -> None:
        def grader(task: Mapping[str, Any], code: str) -> bool:
            return False

        report = _report(_run(StubRouter(), StubTransport(), grader=grader))
        assert report["pairs"][0]["status"] == "failed_pair"
        assert report["savings"]["comparable_pairs"] == 0
        assert report["savings"]["model_choice"]["delta_tokens"] == 0

    def test_cached_tokens_reported_separately(self) -> None:
        """Catches: a cache hit counted as a model-choice saving."""
        transport = StubTransport(
            usage={
                "frontier/big": {"prompt": 1000, "cached": 0, "completion": 100},
                "free/coder": {"prompt": 1000, "cached": 800, "completion": 100},
            }
        )
        report = _report(_run(StubRouter(), transport))
        s = report["savings"]
        assert s["comparable_pairs"] == 1
        # uncached input: baseline 1000, verdict 200 -> model-choice delta excludes cache
        assert s["model_choice"]["baseline_uncached_input"] == 1000
        assert s["model_choice"]["verdict_uncached_input"] == 200
        assert s["cached_input"]["verdict"] == 800
        assert s["cached_input"]["baseline"] == 0
        assert s["model_choice"]["delta_tokens"] == 800

    def test_dollars_only_as_labelled_secondary_line(self) -> None:
        """Catches: dollars presented as the result or as a billed amount."""
        runs = _run(StubRouter(), StubTransport())
        plain = _report(runs)
        assert plain["list_price_equivalent"] is None
        assert "usd" not in json.dumps(plain).lower()
        prices = {
            "frontier/big": {"input_per_1m": 5.0, "output_per_1m": 25.0},
            "free/coder": {"input_per_1m": 0.0, "output_per_1m": 0.0},
        }
        line = _report(runs, list_prices=prices)["list_price_equivalent"]
        assert line["label"] == "list-price equivalent, not an invoice"

    def test_tasks_changed_blocks_claim(self) -> None:
        """Catches: tasks or tests edited during a run."""
        runs = _run(StubRouter(), StubTransport())
        report = _report(runs, integrity={"before": "a", "after": "b"})
        assert report["savings"]["claim_allowed"] is False
        assert any("tasks" in r for r in report["savings"]["reasons"])


# ── Rule 9: flags and bounds ───────────────────────────────────────────


class TestBounds:
    def test_out_dir_inside_repo_is_refused(self) -> None:
        """Catches: writing results into docs/proof again."""
        from scripts.live_savings_bench import validate_args

        with pytest.raises(SystemExit):
            validate_args(_ns(REPO_ROOT / "docs" / "proof" / "x"))

    def test_out_dir_outside_repo_is_accepted(self, tmp_path: Path) -> None:
        from scripts.live_savings_bench import validate_args

        validate_args(_ns(tmp_path / "out"))

    def test_wall_clock_bound_stops_before_next_pair(self) -> None:
        from scripts.live_savings_bench import run_pairs

        ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
        result = run_pairs(
            [TASK, TASK2],
            router=StubRouter(),
            transport=StubTransport(),
            baseline_model="frontier/big",
            repeats=1,
            grader=_always,
            max_wall_seconds=10.0,
            clock=lambda: next(ticks, 100.0),
        )
        assert result.stopped_early
        assert {r.task_id for r in result.runs} == {"t1"}
