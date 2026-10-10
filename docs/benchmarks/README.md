# Benchmarks and measurements

This index lists every benchmark in this repository and what it does and does not show.

**No cost saving is claimed yet.** The only live cost run found no routing saving (see below).
A savings claim returns only with paired live evidence on held-out tasks.

| Benchmark | Kind | Result | Details |
|---|---|---|---|
| Live cost check, 2026-09-28 | live, real models | **no saving**: both arms used the same model | [`docs/proof/live-savings-2026-09-28/`](../proof/live-savings-2026-09-28/) |
| Context packing (#336) | live, one paired run (n=1) | `conclusion=lift` on one check | [context-lift.md](context-lift.md) |
| Paired savings bench | offline fixture | refuses to claim savings (`claims_allowed=false`) | [paired-savings.md](paired-savings.md) |
| Routing demo | deterministic mock | no provider spend | [routing-demo.md](routing-demo.md) |

## Live cost check (negative result)

**No routing saving measured yet.** A live run on real models found that Verdict's
offline router chose the same model as the baseline for every task, so the two arms
cost the same apart from run-to-run token variance.

Setup: fifteen coding tasks (10 standalone + 5 repo-context), each run twice per arm,
graded by executable unit tests. Both arms sent the same prompt.

- **Baseline arm**: every task sent to `cc/claude-opus-5`.
- **Verdict arm**: model chosen by `Gate.route(allow_offline=True)`, the CLI catalog path.
  This path has no live `EligibilityGate`, provider health or quota input, so this run
  does **not** measure the live admission/eligibility router. It chose `cc/claude-opus-5`
  for all 15 tasks.

| Measure (list price x observed tokens) | Overall | Standalone (n=10) | Repo-context (n=5) |
|---|---|---|---|
| Baseline list-price cost | $0.7668 | $0.6225 | $0.1443 |
| Verdict list-price cost | $0.8386 | $0.6756 | $0.1631 |
| (task, repeat) pairs compared | 26 | 20 | 6 |

Same model and same prompt in both arms: the cost difference is token variance between
runs, not a routing effect. Pass rates were 26/30 (baseline arm) and 30/30 (Verdict arm);
with identical model and input this is also run-to-run variance, not a Verdict advantage.
Costs are compared only over (task, repeat) pairs where both arms passed; the four
excluded pairs are cooldown_sentinel r1/r2 and ladder_stages r1/r2 (baseline failed).

Costs are **not billed amounts**: [published list prices](https://www.anthropic.com/pricing)
(fetched at run time; page SHA-256 in the proof dir) applied to observed token usage on
subscription capacity (no invoice). Small n (15 tasks x 2 repeats).

![Live cost check: per-task list-price cost for the baseline arm and the Verdict arm, both on cc/claude-opus-5](../assets/chart-live-savings.svg)

<sub>Data: [`docs/proof/live-savings-2026-09-28/report.json`](../proof/live-savings-2026-09-28/report.json).
Observed token usage x published list prices; subscription capacity, no invoice.
Method: [`scripts/live_savings_bench.py`](../../scripts/live_savings_bench.py);
run with `VERDICT_LIVE_SMOKE=1` (opt-in, spends real capacity).</sub>

Observed data, not a fixture. It is kept to show a negative result, not a saving.
