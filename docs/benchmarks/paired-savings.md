# Paired savings bench: frontier-direct vs Verdict (BOD-101 / BOD-114)

Talk track: **we measure**. The bench compares a frontier-direct arm with the
Verdict cheap-path arm on the same legit task (debug, refactor+tests,
implement-from-AC) and the same acceptance criteria. Savings are only ever
claimed from **executed, receipt-bound evidence**.

## Two modes

| Mode | Command | Can claim savings? |
|------|---------|--------------------|
| `simulation-not-executed` | `verdict benchmark --savings` | **No.** Neither arm runs. Fixture numbers are echoed as *stated* values, every task is withheld with `simulation_not_executed`, and `claims_allowed=false`. |
| `live-paired` | `verdict benchmark --savings --live-paired` | Only when every gate below passes for a task. |

The default is the simulation so the command works offline and from an
installed wheel, but it is labeled as such in the report header, in every
task row, and in the JSON (`mode`, `executed`, `claims_allowed`).

## What a claim requires (live-paired)

For each task, `savings_claimed=true` requires all of:

1. **Both arms executed** through the `execute_arm` hook and returned an
   `execution_id` (OmniRoute `X-OmniRoute-Request-Id`, else body `id`).
2. **Same input hash** — `sha256` of the rendered task message (prompt +
   acceptance criteria) both arms receive. The executor does **not** echo the
   hash it was handed: it recomputes it from the exact request bytes it
   transmitted (`receipt.input_hash_source = "sent_payload"`, with
   `sent_payload_digest`). A stale or tampered task is `input_hash_mismatch`.
3. **Observed cost only** — `X-OmniRoute-Response-Cost`, `-Tokens-In`,
   `-Tokens-Out` headers (or the same fields on a receipt) with the source
   recorded. Nothing is estimated from model names.
4. **Provider-bound identity** — `completed_with` **only** from
   `X-OmniRoute-Model` / `X-OmniRoute-Completed-With`. The response body
   `model` echoes the requested alias and is ignored; with no header the arm
   is `identity_unbound`. For the Verdict arm, `completed_with` must equal
   `routed` (the live chooser's pick) or be a member of the **observed**
   attempt chain reported by the gateway in `X-OmniRoute-Attempt-Chain`
   (comma-separated). The chain Verdict *planned* is recorded as
   `identity_binding.planned_attempt_chain` for explanation only and never
   binds identity. Anything else is `completed_identity_not_in_attempt_chain`
   and refuses.
5. **Quality from output, both arms** — each produced output is evaluated
   against the task's `checks.must_contain`; a task without checks cannot
   pass. A failed frontier baseline is `baseline_quality_miss` (a cheaper
   answer is not savings against an invalid baseline); a failed Verdict
   output is `quality_miss`. `passed=true` together with named misses is
   rejected as contradictory.
6. **Hydrated pack** — the Verdict arm executed the compiled context pack
   (`pack_state=hydrated`, `prompt_digest` recorded) with real
   `included_sources`.
7. **No cache hit** on either arm — a cache hit is labeled
   `cache_hit_is_not_model_savings`, never sold as model savings.
8. **Cost reduction** — `verdict.cost_usd < direct.cost_usd`.

Every refused task lists *all* of its reasons in `withhold_reasons`.

### Response headers a gateway must emit for a claim-capable run

| Header | Purpose | Without it |
|--------|---------|-----------|
| `X-OmniRoute-Request-Id` | `execution_id` | falls back to body `id`; empty → `missing_execution_id` |
| `X-OmniRoute-Model` (or `-Completed-With`) | provider-bound `completed_with` | `identity_unbound` |
| `X-OmniRoute-Response-Cost`, `-Tokens-In`, `-Tokens-Out` | observed cost | `cost_not_observed` |
| `X-OmniRoute-Cache-Hit` | separate cache from model savings | treated as `false` |
| `X-OmniRoute-Attempt-Chain` | observed fallback chain (comma-separated) | any `completed_with ≠ routed` → `completed_identity_not_in_attempt_chain` |

## Clean-environment live run

No source checkout is needed; the fixture and its workspace ship in the wheel.

```bash
python -m venv /tmp/verdict-live && /tmp/verdict-live/bin/pip install "verdict-core"
export OMNIROUTE_BASE_URL="http://<your-omniroute-host>:20128/v1"
export OMNIROUTE_API_KEY="<key>"           # if the gateway requires one
cd /tmp
/tmp/verdict-live/bin/verdict benchmark --savings --live-paired \
  --output-json /tmp/verdict-evidence/paired-savings.json
```

Without `OMNIROUTE_BASE_URL` the `--live-paired` run **exits 2** instead of
silently downgrading to the simulation.

### Running from a Cursor Cloud Agent

The agent VM has no gateway credentials. Add `OMNIROUTE_BASE_URL` and
`OMNIROUTE_API_KEY` as Cloud Agent secrets (Cursor Dashboard → Cloud Agents →
Secrets); they are injected as environment variables into new runs, after
which the command above works unchanged. If the gateway is only reachable
over SSH, tunnel it first:

```bash
ssh -N -L 20128:127.0.0.1:20128 <user>@<vps> &
export OMNIROUTE_BASE_URL="http://127.0.0.1:20128/v1"
```

## Retained evidence bundle (privacy-reviewed)

`report["evidence_bundle"]` holds one row per executed arm and is safe to
retain and share:

- `task_id`, `arm`, `execution_id`, `gateway`, `status_code`
- `input_hash` and `output_digest` (sha256 only — no prompt or output text)
- `completed_with` and the observed `attempt_chain`
- only `x-omniroute-*` / request-id response headers (no `authorization`,
  no request or response bodies)
- the receipt's `usage`, `identity_source`, `input_hash_source`,
  `sent_payload_digest`, and `attempt_chain_source`

Output text is used for the quality evaluation in-process and then discarded.

## Report fields worth reading

```text
mode                 simulation-not-executed | live-paired
claims_allowed       false in simulation
aggregate.executed_count / savings_claimed_count / simulation_count
aggregate.measured_cost_delta_usd   executed arms only
aggregate.stated_cost_delta_usd     fixture-stated (simulation) — not a measurement
tasks[].withhold_reasons            every refusal reason, in precedence order
tasks[].identity_binding            routed → completed_with, bound, explanation
```

## Programmatic use

```python
from verdict.savings_bench import run_savings_bench
from verdict.savings_live import omniroute_arm_executor

report = run_savings_bench(
    execute_arm=omniroute_arm_executor("http://127.0.0.1:20128/v1", api_key="..."),
)
```

Any callable `ArmRequest -> ArmExecution` (see `verdict.savings_execution`)
can be supplied as `execute_arm`; the same evidence gates apply.
