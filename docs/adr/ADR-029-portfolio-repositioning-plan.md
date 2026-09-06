# ADR-029: Portfolio repositioning plan — hygiene, positioning, launch

- **Status:** Accepted
- **Date:** 2026-09-06
- **Deciders:** Nick (repo owner)
- **Related:** [ADR-028](ADR-028-launch-gate-tooling.md); workspace-level ADR "GitHub Portfolio Cleanup" in `/home/nick/dev/CLAUDE-decisions.md` (2026-09-06); tracked via GitHub Project "Verdict Portfolio Plan" (`mrnicholasbcarter-code`, project #5) and issues #439–#448 in this repo

## Context

A five-agent review of the whole GitHub portfolio (2026-09-06) found that `verdict-core` — the
flagship repo for both a job search and a trending-repo push — tracked 860 files of AI-agent
workspace artifacts (767 approval-log JSONs under `evidence/`, a duplicate `Docs/` tree,
`discovery/`, `exports/`, two `CLAUDE.md` backups, a `.mcp.json` leaking a local absolute path,
a dead `node/` stub, a one-off scratch script, and 79 regenerated `.verdict/adaptive_state/`
snapshots), 150+ modules unrelated to routing (`swarm_*`, `memory_*`, `ruflo_*`, `hivemind.py`,
`sona.py`, `neural.py`), an inconsistent pitch across `pyproject.toml`/PyPI/GitHub
description/README, and a README that buries the $0.16-vs-$0.52 cost number instead of leading
with it.

The wider review also concluded that 40k+ stars in 12 months is not a realistic solo-dev
outcome (only LiteLLM clears 40k in the LLM-routing niche); a realistic target is 500–3k stars
in 12 months, 5k–15k with one viral launch. The wedge this repo is actually competing on —
fail-closed routing with a named drop reason and a receipt for every decision — is real but
under-demonstrated, not wrong.

Cross-repo decisions from the same review (repo visibility, the verdict-quant merge, the
verdict-cockpit rebuild direction) are out of scope for this ADR and are recorded at the
workspace level in `CLAUDE-decisions.md`, since they span multiple independent repositories and
this repo has no authority over them.

## Decision

1. **Strip tracked agent-workspace artifacts** (PR #438, issue #440). Remove the 860 files
   listed above and extend `.gitignore` so they cannot return. Verified load-bearing and kept:
   `specs/` (the real Spec Kit record), `.zap/rules.tsv` (wired into the security-scan CI
   workflow), `config/structural_risk_baseline.json` (used by
   `scripts/check_structural_risk.py`), `AGENTS.md` (recognized OSS convention). This is a
   mass-deletion change and is held for the repo owner's explicit merge go-ahead under the
   workspace merge-authority rule, even with green CI.
2. **Relocate non-routing experiments** (issue #441). Move `swarm_*`, `memory_*`, `ruflo_*`,
   `hivemind.py`, `sona.py`, `neural.py` under `verdict/experimental/` or delete them, as a pure
   move with no behavior change, so the top-level package reads as a router.
3. **Unify positioning** (issue #442) on one sentence — "The LLM router that says no: cheapest
   qualified model, a named reason for every drop, a receipt for every decision." — across
   `pyproject.toml`, the PyPI summary, the GitHub repo description, and README line 1.
4. **Reorder the README** (issue #443) to: one-liner → `pip install verdict-core` → a 10-line
   example that runs with no API key and prints a drop reason plus receipt → the $0.16 vs $0.52
   table → the architecture diagram. Keep `quickstart.sh` in sync with the example.
5. **Add visible trust signals** (issue #444): an asciinema/GIF demo at the top of the README, a
   live Codecov badge replacing the static shield, GitHub Discussions enabled, the internal-named
   PR "spec(238)" retitled or closed, and a `v0.3.0` tag once 1–4 land.
6. **Publish a reproducible benchmark** (issue #445): replay one real coding-agent trace through
   always-Opus, a LiteLLM fallback config, and verdict; publish cost, a quality proxy, and the
   full receipt trail for each. This becomes the launch headline.
7. **Ship a LiteLLM custom-router plugin** (issue #446) as a one-import-line distribution
   surface inside an ecosystem this repo does not otherwise reach.
8. **Publish a blog post** (issue #447) covering the benchmark and the cleanup story, for the
   job-search track.
9. **Sequence launch last** (issue #448): `Awesome-Routing-LLMs` PR now (low bar), a coordinated
   48-hour Tuesday–Thursday window (Show HN, r/LocalLLaMA, r/MachineLearning, X, Product Hunt),
   a LangChain/LlamaIndex integration PR the following week, newsletter outreach only after a
   trending day, and an `awesome-model-routing` PR once past 1k stars.

## Consequences

- Steps 1–5 (hygiene, positioning, README, trust signals) must land before steps 6–9 (benchmark,
  plugin, launch) — a launch pointed at an unreviewed repo undermines the wedge instead of
  proving it.
- Step 1 is the only step that deletes tracked history at scale; every other step is additive or
  a pure relocation, so only step 1 needs the explicit merge gate.
- This ADR does not cover `verdict-node`, `verdict-risk`/`verdict-strategy`/`verdict-backtest`
  (→ `verdict-quant`), `verdict-cockpit`, `verdict-ecosystem`, or `prediction-market-sdk` — each
  has its own tracked issue(s) under the same GitHub Project, and the cross-repo decisions
  governing them live in the workspace-level ADR, not here.
- Tracking lives in GitHub Project #5 rather than `.specify/specs/`, since this is portfolio
  operations work (visibility, docs, launch sequencing), not a spec-kit feature change to the
  router itself.
