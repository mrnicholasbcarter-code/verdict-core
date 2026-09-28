#!/usr/bin/env python3
"""Live savings benchmark: real models, real tokens, published list prices.

Opt-in only: refuses to run without VERDICT_LIVE_SMOKE=1.
Uses the OmniRoute gateway at localhost:20128 for API calls.

Usage (from project root, with gateway credentials sourced):
    set -a; . /tmp/.dogfood-env; set +a
    VERDICT_LIVE_SMOKE=1 .venv/bin/python scripts/live_savings_bench.py

Token counters are validated before the benchmark; models with non-proportional
counters are excluded from cost math and named in the report.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
TASKS_JSON = ROOT / "benchmarks" / "fixtures" / "live_savings" / "tasks.json"
PROOF_DIR = ROOT / "docs" / "proof" / "live-savings-2026-09-28"
GATEWAY = "http://localhost:20128/v1"
BASELINE_MODEL = "cc/claude-opus-5"
# Cheaper models Verdict routes simple/medium tasks to
VERDICT_CANDIDATES = ["cc/claude-sonnet-4-5-20250929", "cc/claude-haiku-4-5-20251001"]
EXECUTE_TIMEOUT = 90.0
REPEATS = 2

# Published list prices (USD per 1M tokens)
# Source: https://www.anthropic.com/pricing — accessed 2026-09-28
PRICE_TABLE: dict[str, dict[str, Any]] = {
    "cc/claude-opus-5": {
        "input_per_1m": 15.0,
        "output_per_1m": 75.0,
        "source_url": "https://www.anthropic.com/pricing",
        "access_date": "2026-09-28",
    },
    "cc/claude-sonnet-4-5-20250929": {
        "input_per_1m": 3.0,
        "output_per_1m": 15.0,
        "source_url": "https://www.anthropic.com/pricing",
        "access_date": "2026-09-28",
    },
    "cc/claude-haiku-4-5-20251001": {
        "input_per_1m": 0.80,
        "output_per_1m": 4.0,
        "source_url": "https://www.anthropic.com/pricing",
        "access_date": "2026-09-28",
    },
}


@dataclass
class TokenCounter:
    """Validate token counter proportionality."""

    model: str
    short_tokens: list[int] = field(default_factory=list)
    long_tokens: list[int] = field(default_factory=list)
    proportional: bool = True
    note: str = ""


@dataclass
class TaskResult:
    """Result of one task arm run."""

    task_id: str
    arm: str  # "baseline" or "verdict"
    model: str
    repeat: int
    passed: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    error: str | None = None


def _refuse_without_opt_in() -> None:
    if os.environ.get("VERDICT_LIVE_SMOKE") != "1":
        print("refusing: set VERDICT_LIVE_SMOKE=1 (this spends real capacity)", file=sys.stderr)
        raise SystemExit(2)


def _chat(model: str, prompt: str, *, timeout: float = EXECUTE_TIMEOUT) -> dict[str, Any]:
    """Send a chat completion to the gateway and return the full response."""
    url = f"{GATEWAY}/chat/completions"
    resp = httpx.post(
        url,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "temperature": 0.0,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_code(text: str) -> str:
    """Extract the FIRST Python code block from a model response."""
    # First ```python ... ``` block only (the actual solution)
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Fall back to first ``` ... ```
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Raw text
    return text


def _grade_solution(task: dict[str, Any], code: str) -> bool:
    """Run the task's test file against the solution in a temp sandbox."""
    test_dir = TASKS_JSON.parent
    test_file = test_dir / task["test_file"]
    if not test_file.exists():
        return False

    with tempfile.TemporaryDirectory() as sandbox:
        sandbox_path = Path(sandbox)
        (sandbox_path / "solution.py").write_text(code, encoding="utf-8")
        (sandbox_path / task["test_file"]).write_text(
            test_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(sandbox_path / task["test_file"]),
                "-v",
                "--tb=short",
                "--no-header",
            ],
            capture_output=True,
            text=True,
            cwd=str(sandbox_path),
            timeout=30,
        )
        return result.returncode == 0


def validate_token_counters() -> dict[str, TokenCounter]:
    """Validate each model's token counter with short and long prompts."""
    results: dict[str, TokenCounter] = {}
    models = [BASELINE_MODEL, *VERDICT_CANDIDATES]

    for model in models:
        tc = TokenCounter(model=model)
        short_prompt = "x"  # ~1 token
        long_prompt = "a " * 5000  # ~10k chars, ~2500 tokens

        try:
            for _i in range(3):
                # Cache-bust with unique suffix
                tag = uuid.uuid4().hex[:8]
                r = _chat(model, f"{short_prompt} [{tag}]", timeout=30.0)
                usage = r.get("usage", {})
                tc.short_tokens.append(usage.get("prompt_tokens", 0))

                tag = uuid.uuid4().hex[:8]
                r = _chat(model, f"{long_prompt} [{tag}]", timeout=30.0)
                usage = r.get("usage", {})
                tc.long_tokens.append(usage.get("prompt_tokens", 0))

            avg_short = sum(tc.short_tokens) / len(tc.short_tokens) if tc.short_tokens else 0
            avg_long = sum(tc.long_tokens) / len(tc.long_tokens) if tc.long_tokens else 0

            if avg_short == 0 or avg_long == 0:
                tc.proportional = False
                tc.note = f"zero tokens reported (short={avg_short}, long={avg_long})"
            elif avg_long / avg_short < 5.0:
                tc.proportional = False
                tc.note = (
                    f"not proportional: short_avg={avg_short:.0f}, "
                    f"long_avg={avg_long:.0f}, ratio={avg_long / avg_short:.1f}x"
                )
            else:
                tc.note = (
                    f"OK: short_avg={avg_short:.0f}, "
                    f"long_avg={avg_long:.0f}, ratio={avg_long / avg_short:.1f}x"
                )
        except Exception as e:
            tc.proportional = False
            tc.note = f"error: {e}"

        results[model] = tc
        print(f"  counter {model}: {tc.note}")

    return results


def select_verdict_model(task: dict[str, Any], counter_results: dict[str, TokenCounter]) -> str:
    """Select a model Verdict would route this task class to.

    Simple tasks → cheapest valid candidate (haiku).
    Medium tasks → mid-tier candidate (sonnet).
    Falls back to baseline if all candidates fail validation.
    """
    task_class = task.get("task_class", "medium-code")
    valid = [
        m for m in VERDICT_CANDIDATES if m in counter_results and counter_results[m].proportional
    ]
    if not valid:
        return BASELINE_MODEL
    if "simple" in task_class:
        return min(valid, key=lambda m: PRICE_TABLE.get(m, {}).get("input_per_1m", 999))
    # Medium: prefer sonnet
    for prefer in ["cc/claude-sonnet-4-5-20250929"]:
        if prefer in valid:
            return prefer
    return valid[0]


def compute_cost(result: TaskResult) -> float | None:
    """Compute cost from token usage and published list price."""
    price = PRICE_TABLE.get(result.model)
    if not price:
        return None
    input_cost = result.prompt_tokens * price["input_per_1m"] / 1_000_000
    output_cost = result.completion_tokens * price["output_per_1m"] / 1_000_000
    return input_cost + output_cost


def run_task_arm(task: dict[str, Any], model: str, arm: str, repeat: int) -> TaskResult:
    """Run one task on one model and grade it."""
    # Add cache-busting tag to prompt
    tag = uuid.uuid4().hex[:8]
    prompt = task["prompt"] + f"\n\n<!-- run-{tag} -->"

    start = time.perf_counter()
    try:
        resp = _chat(model, prompt)
        latency = (time.perf_counter() - start) * 1000

        usage = resp.get("usage", {})
        content = ""
        choices = resp.get("choices", [])
        if choices:
            content = (choices[0].get("message") or {}).get("content", "")

        code = _extract_code(content)
        passed = _grade_solution(task, code)

        return TaskResult(
            task_id=task["id"],
            arm=arm,
            model=model,
            repeat=repeat,
            passed=passed,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            latency_ms=latency,
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return TaskResult(
            task_id=task["id"],
            arm=arm,
            model=model,
            repeat=repeat,
            passed=False,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=latency,
            error=str(e),
        )


def build_report(
    results: list[TaskResult], counter_results: dict[str, TokenCounter], tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the final report JSON."""
    ts = datetime.now(timezone.utc).isoformat()
    baseline_results = [r for r in results if r.arm == "baseline"]
    verdict_results = [r for r in results if r.arm == "verdict"]
    baseline_passes = sum(1 for r in baseline_results if r.passed)
    verdict_passes = sum(1 for r in verdict_results if r.passed)
    baseline_passed_ids = {r.task_id for r in baseline_results if r.passed}
    verdict_passed_ids = {r.task_id for r in verdict_results if r.passed}
    both_passed = baseline_passed_ids & verdict_passed_ids
    valid_counter_models = {m for m, tc in counter_results.items() if tc.proportional}

    baseline_cost = 0.0
    verdict_cost = 0.0
    cost_eligible_tasks: list[str] = []
    cost_excluded_tasks: list[dict[str, str]] = []

    for task_id in sorted(both_passed):
        b_runs = [r for r in baseline_results if r.task_id == task_id and r.passed]
        v_runs = [r for r in verdict_results if r.task_id == task_id and r.passed]
        if not b_runs or not v_runs:
            continue
        b = b_runs[0]
        v = v_runs[0]
        b_cost = compute_cost(b)
        v_cost = compute_cost(v)
        if b.model not in valid_counter_models:
            cost_excluded_tasks.append(
                {"task_id": task_id, "reason": f"baseline model {b.model} counter not proportional"}
            )
            continue
        if v.model not in valid_counter_models:
            cost_excluded_tasks.append(
                {"task_id": task_id, "reason": f"verdict model {v.model} counter not proportional"}
            )
            continue
        if b_cost is None or v_cost is None:
            cost_excluded_tasks.append({"task_id": task_id, "reason": "no list price for model"})
            continue
        baseline_cost += b_cost
        verdict_cost += v_cost
        cost_eligible_tasks.append(task_id)

    savings_pct = (
        ((baseline_cost - verdict_cost) / baseline_cost * 100) if baseline_cost > 0 else 0.0
    )

    per_task: list[dict[str, Any]] = []
    for task_id in sorted({r.task_id for r in results}):
        task_results = [r for r in results if r.task_id == task_id]
        entry: dict[str, Any] = {"task_id": task_id, "runs": []}
        for tr in task_results:
            cost = compute_cost(tr)
            run_entry: dict[str, Any] = {
                "arm": tr.arm,
                "model": tr.model,
                "repeat": tr.repeat,
                "passed": tr.passed,
                "prompt_tokens": tr.prompt_tokens,
                "completion_tokens": tr.completion_tokens,
                "total_tokens": tr.total_tokens,
                "latency_ms": round(tr.latency_ms, 1),
            }
            if cost is not None:
                run_entry["cost_usd"] = round(cost, 6)
            if tr.error:
                run_entry["error"] = tr.error
            entry["runs"].append(run_entry)
        per_task.append(entry)

    counter_detail = {}
    for model, tc in counter_results.items():
        counter_detail[model] = {
            "proportional": tc.proportional,
            "short_prompt_tokens": tc.short_tokens,
            "long_prompt_tokens": tc.long_tokens,
            "note": tc.note,
        }

    failures: list[dict[str, str]] = []
    for r in results:
        if not r.passed:
            fail: dict[str, str] = {
                "task_id": r.task_id,
                "arm": r.arm,
                "model": r.model,
                "repeat": str(r.repeat),
            }
            if r.error:
                fail["error"] = r.error
            failures.append(fail)

    return {
        "schema_version": "1",
        "generated_at": ts,
        "label": (
            "Measured cost comparison using real model runs, real observed token usage, "
            "priced at the provider's published per-token list prices. "
            "Subscription capacity; no invoice — costs are computed, not billed."
        ),
        "methodology": {
            "baseline_model": BASELINE_MODEL,
            "verdict_routing": "task-class routing to cheaper model when task is simple/medium",
            "grading": "unit tests in temp sandbox",
            "repeats": REPEATS,
            "note": "Small n; two repeats per arm per task.",
        },
        "token_counter_validation": counter_detail,
        "price_table": PRICE_TABLE,
        "summary": {
            "total_runs": len(results),
            "baseline_pass_rate": f"{baseline_passes}/{len(baseline_results)}",
            "verdict_pass_rate": f"{verdict_passes}/{len(verdict_results)}",
            "cost_eligible_tasks": cost_eligible_tasks,
            "cost_excluded_tasks": cost_excluded_tasks,
            "baseline_cost_usd": round(baseline_cost, 6),
            "verdict_cost_usd": round(verdict_cost, 6),
            "savings_pct": round(savings_pct, 1),
            "savings_label": (
                "Savings computed only over tasks where both arms passed, "
                "using models with validated token counters and published list prices."
            ),
        },
        "failures": failures,
        "per_task": per_task,
    }


def sanitize_receipt(result: TaskResult) -> dict[str, Any]:
    """Create a sanitized per-call receipt (no prompts, outputs, keys, headers)."""
    entry: dict[str, Any] = {
        "task_id": result.task_id,
        "arm": result.arm,
        "model": result.model,
        "repeat": result.repeat,
        "passed": result.passed,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "latency_ms": round(result.latency_ms, 1),
    }
    cost = compute_cost(result)
    if cost is not None:
        entry["list_price_cost_usd"] = round(cost, 6)
    if result.error:
        entry["error"] = result.error
    return entry


def main() -> None:
    _refuse_without_opt_in()

    tasks_data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
    tasks = tasks_data["tasks"]

    print("=== Live savings benchmark ===")
    print(f"Tasks: {len(tasks)}")
    print(f"Baseline: {BASELINE_MODEL}")
    print(f"Repeats per arm: {REPEATS}")
    print()

    # Step 1: Validate token counters
    print("--- Token counter validation ---")
    counter_results = validate_token_counters()
    excluded_models = {m for m, tc in counter_results.items() if not tc.proportional}
    if excluded_models:
        print(f"  EXCLUDED from cost math: {excluded_models}")
    print()

    # Step 2: Run tasks
    print("--- Running tasks ---")
    all_results: list[TaskResult] = []
    for task in tasks:
        verdict_model = select_verdict_model(task, counter_results)
        print(f"  {task['id']}: baseline={BASELINE_MODEL}, verdict={verdict_model}")

        for repeat in range(1, REPEATS + 1):
            print(f"    repeat {repeat} baseline...", end=" ", flush=True)
            br = run_task_arm(task, BASELINE_MODEL, "baseline", repeat)
            print(
                f"{'PASS' if br.passed else 'FAIL'} "
                f"({br.prompt_tokens}+{br.completion_tokens} tok, "
                f"{br.latency_ms:.0f}ms)"
            )
            all_results.append(br)

            print(f"    repeat {repeat} verdict...", end=" ", flush=True)
            vr = run_task_arm(task, verdict_model, "verdict", repeat)
            print(
                f"{'PASS' if vr.passed else 'FAIL'} "
                f"({vr.prompt_tokens}+{vr.completion_tokens} tok, "
                f"{vr.latency_ms:.0f}ms)"
            )
            all_results.append(vr)
    print()

    # Step 3: Build report
    report = build_report(all_results, counter_results, tasks)

    # Step 4: Write outputs
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    report_path = PROOF_DIR / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {report_path}")

    receipts_path = PROOF_DIR / "receipts.json"
    receipts = [sanitize_receipt(r) for r in all_results]
    receipts_path.write_text(json.dumps(receipts, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {receipts_path}")

    # Summary
    s = report["summary"]
    print()
    print("=== Summary ===")
    print(f"Baseline pass rate: {s['baseline_pass_rate']}")
    print(f"Verdict pass rate:  {s['verdict_pass_rate']}")
    print(f"Cost-eligible tasks: {len(s['cost_eligible_tasks'])}")
    if s["cost_excluded_tasks"]:
        print(f"Cost-excluded tasks: {len(s['cost_excluded_tasks'])}")
        for ex in s["cost_excluded_tasks"]:
            print(f"  {ex['task_id']}: {ex['reason']}")
    print(f"Baseline cost (list price): ${s['baseline_cost_usd']:.4f}")
    print(f"Verdict cost (list price):  ${s['verdict_cost_usd']:.4f}")
    if s["baseline_cost_usd"] > 0 and s["savings_pct"] > 0:
        print(f"Savings: {s['savings_pct']}%")
    elif s["baseline_cost_usd"] > 0:
        print("No savings (or quality loss)")
    else:
        print("No cost-eligible tasks")
    print()
    print("Note: Subscription capacity — no invoice. Costs computed from published")
    print("list prices applied to observed token usage, not billed amounts.")

    if report["failures"]:
        print()
        print("=== Failures ===")
        for f in report["failures"]:
            parts = [f"  {f['task_id']} {f['arm']} r{f['repeat']} ({f.get('model', '?')})"]
            if "error" in f:
                parts.append(f": {f['error'][:80]}")
            print("".join(parts))


if __name__ == "__main__":
    main()
