# Live dogfood proof: worker failover and controller resume (BOD-225 / BOD-70)

A live `verdict orchestrate` dogfood run for Linear story BOD-225
(which added producer provenance to receipts), tracked as BOD-70 proof work.
The historical receipt in this bundle does not attest the engine SHA or
PYTHONPATH. Providers were reached through OmniRoute on real subscription
capacity.

**Goal**: BOD-225 — record the producing Verdict version and git SHA in run
receipts (`producer {verdict_version, git_sha, dirty}`), keep the original
producer across resume, and extend `certify_release.py` rehearsal checks.

**Engine SHA (capture-author account)**: `0008b8d` (reportedly origin/main at
`run_started` and pinned via PYTHONPATH). The events identify `0008b8d` as a
worker repository base, not as the producing interpreter or imported engine SHA.

**Models** (live via OmniRoute):
- Planner: `cx/gpt-5.5` then `cx/gpt-5.6-sol` (planner `no_final_answer` failover)
- Workers: `cx/gpt-5.5`, `cx/gpt-5.6-sol`, `gc/grok-4.5` (successful work on grok)
- Review: `cc/claude-opus-4-6` (open-code-review v1.12.9)

**Wall time**: ~1h 49m (`2026-09-29T02:43:27Z` → `2026-09-29T04:32:35Z`)

**Recorded review verdict**: PASS (76.6 seconds; 0 blocking). The raw OCR output
and coverage are not retained here, so this is not independently auditable proof
of semantic review.

**Worker commits** (branch `dogfood/bod-225b-live`):
- `0bea0a3` — `producer_receipt` on `gc/grok-4.5`
- `b005418` — `rehearsal_guard` on `gc/grok-4.5`
- `fdd0ce3` — `integrate_provenance` (mechanical combine)

**Diff**: +669 / −6 across 6 files. `no_change_nodes`: none.

## Chain (112 events; condensed)

| seq | event | node | route | outcome |
|---|---|---|---|---|
| 1 | run_started | | | fault-injecting; resumed=[] |
| 4–8 | planner | | cx/gpt-5.5→cx/gpt-5.6-sol | no_final_answer then HEALTHY |
| 20–27 | reassign | producer_receipt | cx/gpt-5.6-sol→gc/grok-4.5 | injected rate_limited |
| 37 | VALIDATED | producer_receipt | gc/grok-4.5 | 0bea0a3 verify PASS |
| 45–52 | reassign | rehearsal_guard | cx/gpt-5.5→cx/gpt-5.6-sol | timeout |
| 58–65 | reassign | rehearsal_guard | cx/gpt-5.6-sol→gc/grok-4.5 | no_final_answer |
| 71–72 | RESUMED | | | 1 validated node reused |
| 75 | ABANDONED | rehearsal_guard | | 3 prior attempts kept |
| 89 | VALIDATED | rehearsal_guard | gc/grok-4.5 | b005418 verify PASS |
| 97–99 | integrate | integrate_provenance | mechanical | fdd0ce3 combined verify PASS |
| 100 | REVIEW_INDEPENDENCE | | | reviewer empty; reboot is operator-reported |
| 101–102 | RESUMED | | | 3 validated nodes reused |
| 107–108 | integrate/barrier | | | reused fdd0ce3 |
| 109–111 | review | | cc/claude-opus-4-6 | PASS |
| 112 | run_finished | | | COMPLETE |

Controller-verified failures: seq 22 `cx/gpt-5.6-sol` rate_limited (injected
`worker#1`); seq 47 `cx/gpt-5.5` timeout (real); seq 60 `cx/gpt-5.6-sol`
`no_final_answer` (real); plus planner `no_final_answer` at seq 4 (real).
Four cooldowns and three reassignments (seq 27, 52, 65), each replacement on a
different route.

## What this proves

Evidence in this bundle shows:

- worker assignment across live OmniRoute providers;
- failover on one injected fault and two real worker faults, with cooldown and
  reassignment to a different route;
- controller survival across restart and resume, reusing already-validated
  nodes (seq 71 and seq 101); a VM reboot is operator-reported, not
  established by the retained events;
- a recorded reviewer PASS on `cc/claude-opus-4-6`, a route no worker used;
  raw OCR output and coverage are not retained, so semantic review is unverified;
- a verified receipt (`verdict run-receipt` → COMPLETE, integrity OK).

## What this does NOT prove

- The reviewer is `cc/claude-opus-4-6`, which is also the controller-family
  model used by this session's merge captain (not by the run's workers).
- It is a single run.
- The cause of resume is not established by this bundle. The capture author
  reports a VM reboot rather than an injected controller crash.

## How to verify

```bash
verdict run-receipt docs/proof/dogfood-bod-225-live-2026-09-29
```

Expected: COMPLETE with integrity OK. Worker code lives on branch
`dogfood/bod-225b-live` (SHAs above; branch tip at audit:
`0c21c6d859355fc8f8482baf9f0779ba63fed2c0`). That worker code was
later ported and merged to main via PR #784; the historical run remains tied
to this worker branch, not the later main merge.

Capture-host home-directory and `/tmp` run paths appear inside `events.jsonl`
(for example resolved interpreter argv0 and worktree paths). They are part of
the digest-bound event log and are left unedited per receipt integrity
requirements. Do not rewrite those bytes.
