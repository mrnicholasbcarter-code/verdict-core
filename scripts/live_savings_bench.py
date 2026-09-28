#!/usr/bin/env python3
"""Live savings benchmark: real models, real tokens, published list prices.

Opt-in only: refuses to run without VERDICT_LIVE_SMOKE=1.
Uses the OmniRoute gateway at localhost:20128 for API calls.
Verdict arm calls the real Gate.route() path (legacy feed / offline catalog).

Prices are fetched at run time from https://www.anthropic.com/pricing and parsed
from the HTML. The fetched page text and its SHA-256 are stored alongside the
report so results can be audited.

Usage (from project root, with gateway credentials sourced):
    set -a; . /tmp/.dogfood-env; set +a
    VERDICT_LIVE_SMOKE=1 PYTHONPATH=/tmp/vsav .venv/bin/python scripts/live_savings_bench.py

Token counters are validated before the benchmark; models with non-proportional
counters are excluded from cost math and named in the report.
"""

from __future__ import annotations

import hashlib
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

# ── Configuration ──────────────────────────────────────────────────────
GATEWAY = os.environ.get("OMNIROUTE_GATEWAY", "http://localhost:20128/v1")
TASKS_JSON = Path(__file__).resolve().parent.parent / "benchmarks" / "fixtures" / "live_savings" / "tasks.json"
PROOF_DIR = Path(__file__).resolve().parent.parent / "docs" / "proof" / "live-savings-2026-09-28"
REPEATS = 2
EXECUTE_TIMEOUT = 120.0

BASELINE_MODEL = "cc/claude-opus-5"
VERDICT_CANDIDATES = ["cc/claude-sonnet-4-5-20250929", "cc/claude-haiku-4-5-20251001"]

# Price URL
PRICING_URL = "https://www.anthropic.com/pricing"

# Model name -> display name mapping for parsing
MODEL_PRICE_KEYS: dict[str, str] = {
    "cc/claude-opus-5": "Opus 5",
    "cc/claude-sonnet-4-5-20250929": "Sonnet 4.5",
    "cc/claude-haiku-4-5-20251001": "Haiku 4.5",
}


# ── Price fetching ─────────────────────────────────────────────────────
def fetch_prices() -> tuple[dict[str, dict[str, Any]], str, str]:
    """Fetch and parse prices from anthropic.com/pricing.

    Returns (price_table, page_sha256, access_timestamp).
    Raises RuntimeError if a model price cannot be found.
    """
    resp = httpx.get(PRICING_URL, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    page_text = resp.text
    page_sha256 = hashlib.sha256(page_text.encode()).hexdigest()
    access_ts = datetime.now(timezone.utc).isoformat()

    # Store snapshot for auditability
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = PROOF_DIR / "pricing-page-snapshot.txt"
    # Write a trimmed version (strip large SVG/style blocks but keep pricing text)
    # Actually store the sha256 and key extracted text, not the full 1MB page
    extracted: list[str] = []

    price_table: dict[str, dict[str, Any]] = {}

    for model_id, display_name in MODEL_PRICE_KEYS.items():
        # Find model card section: "Opus 5" followed by Input $X / MTok and Output $Y / MTok
        # Escape for regex, handle "Opus 5" not matching "Opus 5.5"
        if display_name.endswith("5"):
            pattern = re.escape(display_name) + r'(?![\.\d])'
        else:
            pattern = re.escape(display_name)

        found = False
        for m in re.finditer(pattern, page_text, re.IGNORECASE):
            end = min(len(page_text), m.end() + 3000)
            chunk = page_text[m.start():end]
            # Strip HTML tags for easier parsing
            clean = re.sub(r'<[^>]+>', '|', chunk)
            clean = re.sub(r'\|+', '|', clean)

            input_match = re.search(r'Input[\|\s]*\$(\d+(?:\.\d+)?)[\s\|]*/\s*MTok', clean)
            output_match = re.search(r'Output[\|\s]*\$(\d+(?:\.\d+)?)[\s\|]*/\s*MTok', clean)
            if input_match and output_match:
                input_price = float(input_match.group(1))
                output_price = float(output_match.group(1))
                price_table[model_id] = {
                    "input_per_1m": input_price,
                    "output_per_1m": output_price,
                    "source_url": PRICING_URL,
                    "access_date": access_ts,
                    "parsed_from": display_name,
                }
                extracted.append(
                    f"{display_name}: Input ${input_price}/MTok, Output ${output_price}/MTok"
                )
                found = True
                break

        if not found:
            raise RuntimeError(
                f"Cannot find price for {display_name} ({model_id}) on {PRICING_URL}. "
                f"Page SHA-256: {page_sha256}"
            )

    # Write audit file
    snapshot_path.write_text(
        f"# Pricing page snapshot\n"
        f"URL: {PRICING_URL}\n"
        f"Fetched: {access_ts}\n"
        f"SHA-256: {page_sha256}\n"
        f"Page size: {len(page_text)} chars\n\n"
        f"## Extracted prices\n" +
        "\n".join(extracted) + "\n",
        encoding="utf-8",
    )

    return price_table, page_sha256, access_ts


# ── Data classes ───────────────────────────────────────────────────────
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
    routing_receipt: dict[str, Any] | None = None


# ── Helpers ────────────────────────────────────────────────────────────
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
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
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
        short_prompt = "x"
        long_prompt = "a " * 5000

        try:
            for _i in range(3):
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


# ── Verdict routing (real Gate.route path) ─────────────────────────────
def select_verdict_model(
    task: dict[str, Any],
    counter_results: dict[str, TokenCounter],
    price_table: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Use the real Verdict Gate.route() to select a model.

    Returns (model_id, routing_receipt_dict).
    The routing receipt records the decision reason, tier, alternatives, etc.
    Candidate pool is limited to models with validated token counters and sourced prices.
    """
    from verdict.gate import Gate

    # Build a Gate with allow_offline=True (static catalog, no probe I/O)
    # and only the models we have valid counters + prices for
    valid_models = {BASELINE_MODEL} | {
        m for m in VERDICT_CANDIDATES
        if m in counter_results and counter_results[m].proportional and m in price_table
    }

    gate = Gate(
        primary_model=BASELINE_MODEL,
        allow_offline=True,
    )

    task_prompt = task.get("prompt", "")
    task_class = task.get("task_class", "medium-code")

    # Map task_class to criticality for the router
    criticality = "medium"
    if "simple" in task_class:
        criticality = "low"

    try:
        decision = gate.route(task_prompt[:500], criticality=criticality)

        # Build receipt
        receipt: dict[str, Any] = {
            "selected_model": decision.model,
            "provider": decision.provider,
            "tier": decision.tier,
            "reason": decision.reason,
            "decision": decision.decision,
            "alternatives": decision.alternatives[:3] if decision.alternatives else [],
            "task_class": decision.task_class,
            "safety_flags": decision.safety_flags,
        }

        model = decision.model
        # If the routed model is not in our valid pool, fall back to baseline
        if model not in valid_models:
            receipt["fallback_reason"] = f"routed model {model} not in valid pool {valid_models}"
            model = BASELINE_MODEL

        return model, receipt

    except Exception as e:
        receipt = {"error": str(e), "fallback": "baseline"}
        return BASELINE_MODEL, receipt


def compute_cost(result: TaskResult, price_table: dict[str, dict[str, Any]]) -> float | None:
    """Compute cost from token usage and published list price."""
    price = price_table.get(result.model)
    if not price:
        return None
    input_cost = result.prompt_tokens * price["input_per_1m"] / 1_000_000
    output_cost = result.completion_tokens * price["output_per_1m"] / 1_000_000
    return input_cost + output_cost


def run_task_arm(task: dict[str, Any], model: str, arm: str, repeat: int,
                 routing_receipt: dict[str, Any] | None = None) -> TaskResult:
    """Run one task on one model and grade it."""
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
            routing_receipt=routing_receipt,
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
            routing_receipt=routing_receipt,
        )


def build_report(
    results: list[TaskResult],
    counter_results: dict[str, TokenCounter],
    tasks: list[dict[str, Any]],
    price_table: dict[str, dict[str, Any]],
    price_sha256: str,
    price_access_ts: str,
) -> dict[str, Any]:
    """Build the final report JSON with per-(task,repeat) eligibility and per-group results."""
    ts = datetime.now(timezone.utc).isoformat()
    baseline_results = [r for r in results if r.arm == "baseline"]
    verdict_results = [r for r in results if r.arm == "verdict"]
    baseline_passes = sum(1 for r in baseline_results if r.passed)
    verdict_passes = sum(1 for r in verdict_results if r.passed)
    valid_counter_models = {m for m, tc in counter_results.items() if tc.proportional}

    # Per-(task, repeat) eligibility: both arms must pass for that specific pair
    baseline_cost = 0.0
    verdict_cost = 0.0
    cost_eligible_pairs: list[dict[str, Any]] = []
    cost_excluded_pairs: list[dict[str, Any]] = []

    task_ids = sorted({r.task_id for r in results})
    repeats = sorted({r.repeat for r in results})

    for task_id in task_ids:
        for repeat in repeats:
            b_run = next(
                (r for r in baseline_results if r.task_id == task_id and r.repeat == repeat),
                None,
            )
            v_run = next(
                (r for r in verdict_results if r.task_id == task_id and r.repeat == repeat),
                None,
            )
            pair_key = f"{task_id}/r{repeat}"

            if b_run is None or v_run is None:
                cost_excluded_pairs.append({"pair": pair_key, "reason": "missing arm run"})
                continue
            if not b_run.passed:
                cost_excluded_pairs.append({"pair": pair_key, "reason": "baseline failed"})
                continue
            if not v_run.passed:
                cost_excluded_pairs.append({"pair": pair_key, "reason": "verdict failed"})
                continue
            if b_run.model not in valid_counter_models:
                cost_excluded_pairs.append({
                    "pair": pair_key,
                    "reason": f"baseline model {b_run.model} counter not proportional",
                })
                continue
            if v_run.model not in valid_counter_models:
                cost_excluded_pairs.append({
                    "pair": pair_key,
                    "reason": f"verdict model {v_run.model} counter not proportional",
                })
                continue

            b_cost = compute_cost(b_run, price_table)
            v_cost = compute_cost(v_run, price_table)
            if b_cost is None or v_cost is None:
                cost_excluded_pairs.append({"pair": pair_key, "reason": "no list price for model"})
                continue

            baseline_cost += b_cost
            verdict_cost += v_cost
            cost_eligible_pairs.append({
                "pair": pair_key,
                "baseline_cost": round(b_cost, 6),
                "verdict_cost": round(v_cost, 6),
            })

    savings_pct = (
        ((baseline_cost - verdict_cost) / baseline_cost * 100) if baseline_cost > 0 else 0.0
    )

    # Per-group breakdown
    task_groups: dict[str, list[str]] = {}
    for t in tasks:
        g = t.get("task_group", "standalone")
        task_groups.setdefault(g, []).append(t["id"])

    group_summaries: dict[str, dict[str, Any]] = {}
    for group, group_task_ids in sorted(task_groups.items()):
        g_baseline = 0.0
        g_verdict = 0.0
        g_eligible = 0
        g_total_pairs = 0
        g_baseline_pass = 0
        g_verdict_pass = 0
        g_baseline_total = 0
        g_verdict_total = 0
        for task_id in group_task_ids:
            b_runs = [r for r in baseline_results if r.task_id == task_id]
            v_runs = [r for r in verdict_results if r.task_id == task_id]
            g_baseline_pass += sum(1 for r in b_runs if r.passed)
            g_verdict_pass += sum(1 for r in v_runs if r.passed)
            g_baseline_total += len(b_runs)
            g_verdict_total += len(v_runs)
            for p in cost_eligible_pairs:
                if p["pair"].split("/")[0] == task_id:
                    g_baseline += p["baseline_cost"]
                    g_verdict += p["verdict_cost"]
                    g_eligible += 1
            for repeat in repeats:
                g_total_pairs += 1

        g_savings = (
            ((g_baseline - g_verdict) / g_baseline * 100) if g_baseline > 0 else 0.0
        )
        group_summaries[group] = {
            "tasks": group_task_ids,
            "eligible_pairs": g_eligible,
            "total_pairs": g_total_pairs,
            "baseline_cost_usd": round(g_baseline, 6),
            "verdict_cost_usd": round(g_verdict, 6),
            "savings_pct": round(g_savings, 1),
            "baseline_pass_rate": f"{g_baseline_pass}/{g_baseline_total}",
            "verdict_pass_rate": f"{g_verdict_pass}/{g_verdict_total}",
        }

    per_task: list[dict[str, Any]] = []
    for task_id in sorted({r.task_id for r in results}):
        task_results = [r for r in results if r.task_id == task_id]
        entry: dict[str, Any] = {"task_id": task_id, "runs": []}
        for tr in task_results:
            cost = compute_cost(tr, price_table)
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
            if tr.routing_receipt:
                run_entry["routing_receipt"] = tr.routing_receipt
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
        "schema_version": "2",
        "generated_at": ts,
        "label": (
            "Measured cost comparison using real model runs, real observed token usage, "
            "priced at the provider's published per-token list prices. "
            "Verdict arm uses Gate.route() (legacy feed / offline catalog). "
            "Subscription capacity; no invoice — costs are computed, not billed."
        ),
        "methodology": {
            "baseline_model": BASELINE_MODEL,
            "verdict_routing": "Gate.route() with allow_offline=True (real planner + catalog ranker)",
            "grading": "unit tests in temp sandbox",
            "repeats": REPEATS,
            "eligibility": "per (task, repeat) pair — both arms must pass",
            "note": "Small n; two repeats per arm per task.",
        },
        "pricing_source": {
            "url": PRICING_URL,
            "page_sha256": price_sha256,
            "fetched_at": price_access_ts,
        },
        "token_counter_validation": counter_detail,
        "price_table": price_table,
        "summary": {
            "total_runs": len(results),
            "baseline_pass_rate": f"{baseline_passes}/{len(baseline_results)}",
            "verdict_pass_rate": f"{verdict_passes}/{len(verdict_results)}",
            "cost_eligible_pairs": [p["pair"] for p in cost_eligible_pairs],
            "cost_excluded_pairs": cost_excluded_pairs,
            "baseline_cost_usd": round(baseline_cost, 6),
            "verdict_cost_usd": round(verdict_cost, 6),
            "savings_pct": round(savings_pct, 1),
            "savings_label": (
                "Savings computed only over (task, repeat) pairs where both arms passed, "
                "using models with validated token counters and published list prices."
            ),
        },
        "group_summaries": group_summaries,
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
    if result.routing_receipt:
        entry["routing_receipt"] = result.routing_receipt
    if result.error:
        entry["error"] = result.error
    return entry


def main() -> None:
    _refuse_without_opt_in()

    # Step 0: Fetch prices from the live page
    print("--- Fetching prices from anthropic.com/pricing ---")
    price_table, price_sha256, price_access_ts = fetch_prices()
    for model_id, prices in price_table.items():
        print(f"  {model_id}: ${prices['input_per_1m']}/MTok in, ${prices['output_per_1m']}/MTok out")
    print(f"  Page SHA-256: {price_sha256[:16]}...")
    print()

    tasks_data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
    tasks = tasks_data["tasks"]

    print("=== Live savings benchmark ===")
    print(f"Tasks: {len(tasks)} ({sum(1 for t in tasks if t.get('task_group')=='standalone')} standalone, "
          f"{sum(1 for t in tasks if t.get('task_group')=='repo-context')} repo-context)")
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
        verdict_model, routing_receipt = select_verdict_model(task, counter_results, price_table)
        group = task.get("task_group", "standalone")
        print(f"  {task['id']} [{group}]: baseline={BASELINE_MODEL}, verdict={verdict_model}")
        if routing_receipt:
            reason = routing_receipt.get("reason", "?")
            print(f"    route: {reason[:80]}")

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
            vr = run_task_arm(task, verdict_model, "verdict", repeat, routing_receipt)
            print(
                f"{'PASS' if vr.passed else 'FAIL'} "
                f"({vr.prompt_tokens}+{vr.completion_tokens} tok, "
                f"{vr.latency_ms:.0f}ms)"
            )
            all_results.append(vr)
    print()

    # Step 3: Build report
    report = build_report(all_results, counter_results, tasks, price_table, price_sha256, price_access_ts)

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
    print(f"Cost-eligible pairs: {len(s['cost_eligible_pairs'])}")
    if s["cost_excluded_pairs"]:
        print(f"Cost-excluded pairs: {len(s['cost_excluded_pairs'])}")
        for ex in s["cost_excluded_pairs"]:
            print(f"  {ex['pair']}: {ex['reason']}")
    print(f"Baseline cost (list price): ${s['baseline_cost_usd']:.4f}")
    print(f"Verdict cost (list price):  ${s['verdict_cost_usd']:.4f}")
    if s["baseline_cost_usd"] > 0 and s["savings_pct"] > 0:
        print(f"Savings: {s['savings_pct']}%")
    elif s["baseline_cost_usd"] > 0:
        print("No savings (or quality loss)")
    else:
        print("No cost-eligible pairs")

    # Per-group
    print()
    print("=== Per-group results ===")
    for group, gs in report.get("group_summaries", {}).items():
        print(f"  {group} ({len(gs['tasks'])} tasks):")
        print(f"    Baseline pass: {gs['baseline_pass_rate']}, Verdict pass: {gs['verdict_pass_rate']}")
        print(f"    Eligible pairs: {gs['eligible_pairs']}/{gs['total_pairs']}")
        print(f"    Baseline cost: ${gs['baseline_cost_usd']:.4f}, Verdict cost: ${gs['verdict_cost_usd']:.4f}")
        if gs["baseline_cost_usd"] > 0:
            print(f"    Savings: {gs['savings_pct']}%")

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
