# Proposal: A held coordinator lease skips probes but still selects proven routes

## Why

While the six-hourly screen (`verdict identify --screen`) holds `verified-refresh.lock` (about 10 minutes), every route selection on the machine returns nothing: `choose` and `route` say `no_eligible_route` and `verdict serve` can return HTTP 503 `fail_closed — empty active ∩ fresh-passport ∩ confirmed intersection`. The hourly acceptance run fails at 00/06/12/18 UTC because of it, which turns `verdict doctor` red.

Cause (verified in source on `origin/main`): `verdict/orchestration/eligibility.py` `EligibilityLadder.select`, around lines 1214-1222: `if _JobLock(_lock_path).live(): probe_order = []`. That loop (around 1223-1290) is also the only place `chosen` is assigned, so emptying it removes every candidate, including routes already proven healthy that need no probe. The intent (BOD-297 comment) was only "never probe while another producer owns the job".

Same pattern in `verdict/admit_prove_confirm.py` around 605-632: lease live -> every shortlisted id `confirm_unavailable` and `admitted=()`.

Fix it so a held lease skips probes only; routes that are already proven (healthy, launchable, fresh proof) are still selected and admitted.

## What Changes

- `EligibilityLadder.select`: when the coordinator lease is held, do not empty `probe_order`. Instead, set a `lease_held` flag. In the probe loop, where a probe would be sent, if `lease_held` is true, set the candidate's reason to `lease_held` and continue without probing. Routes that are already healthy, launchable, and have fresh proof will still be chosen because they don't enter the probe-sending branch.
- `gate_admit_prove_confirm`: when the lease is held, admit routes with fresh passports (already qualified) and mark only stale/no-passport routes as `confirm_unavailable`. Do not empty the admitted set entirely.
- `verdict/acceptance.py` `judge_choose`: take the failure reason from stdout JSON when present, else the last stderr line that does not start with `outcome history:`.

## Capabilities

### Modified Capabilities

- `shared-selection`: Selection still picks the same first route across surfaces when a lease is held, because proven routes are still admitted and chosen.
- `shared-qualification`: The coordinator lease no longer blocks admission of routes with fresh proof; only probes and confirmations are skipped.

## Impact

- `verdict/orchestration/eligibility.py` `EligibilityLadder.select`
- `verdict/admit_prove_confirm.py` `gate_admit_prove_confirm`
- `verdict/acceptance.py` `judge_choose`
- New behavioral tests in existing test files for eligibility and admit_prove_confirm
- No API, gate, dependency, or install change.

## Evidence boundary

KNOWN: root cause analysis (Opus, read-only) confirmed screen and hourly acceptance co-start at the same second; acceptance finishes ~+2 min, inside the screen's ~10 min lock; 180013Z eligibility had 18 eligible routes (supply fine). Rate limits and non-atomic cache writes ruled out (health_cache.py 1114-1132 uses temp file + os.replace). KNOWN: the BOD-297 comment states intent was only "never probe while another producer owns the job". INFERRED: keeping `probe_order` but skipping probe dispatch when `lease_held` preserves selection of proven routes. NOT AVAILABLE: installed PyPI proof, live gateway acceptance run. Local OpenSpec and test output are not those proofs.

## Acceptance criteria

- While another producer holds the coordinator lease, selection and admission send no probe and no confirmation.
- Routes whose health and agentic proof are already fresh are still selected and admitted.
- Routes that would need a probe are reported with reason `lease_held`.
- Both validators exit 0: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/lease-held-selection` ; `npx --yes @fission-ai/openspec@1.13.2 validate lease-held-selection --strict --no-interactive`.
- All gate commands exit 0 (except `check_openspec_tasks` may report only the post-merge task).