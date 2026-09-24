# Verdict Core — evidence-backed showcase package

## Announcement draft (branch-qualified)

**Verdict Core demonstrates goal-to-receipt orchestration with capacity-aware model selection and inspectable failure recovery.**

On the `feat/interview-golden-path` branch, Verdict takes a development goal through frontier planning, parallel work units, integration, independent AI code review and a verified run receipt. Verdict owns eligibility and route selection. Prime Agent executes workers. OmniRoute supplies inventory and transport. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) defines those boundaries.

This is a certified branch demonstration, **not** a claim of production deployment or merge to `main`. A [fresh-clone certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) records a clean install, `ruff check .`, `mypy --strict verdict/`, **2907 passed** in the full test suite and a live `certlive` run that completed with an integration barrier and a passing independent review. The evidence directory is `~/.verdict/evidence/golden-path/` (local operator artifacts, not checked into the repository).

### What is shown

- **Dynamic, capacity-aware selection:** live discovery, entitlement, health and cooldown checks precede task eligibility and ranking. Capacity class reflects account evidence, not a static provider fallback list. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).
- **Bounded same-node recovery:** quota, 401/402/403, 429, timeout, transport and 5xx scenarios were exercised on live `cc/*` and `cx/*` capacity with tagged fault injections. Receipts name route changes and cooldown scope; `live7` and `live11` demonstrate fail-closed pool exhaustion. [Scenario matrix](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).
- **Independent review and fail-closed receipt:** after integration, OpenCodeReview reviews the diff on a route that excludes implementers. Missing or failed review cannot produce `COMPLETE`. `events.jsonl` has a digest in `receipt.json`; the completed live runs passed receipt integrity checks. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md); [certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).

The source is at [mrnicholasbcarter-code/verdict-core](https://github.com/mrnicholasbcarter-code/verdict-core).

## Five-minute showcase script

Prerequisites and the full procedure are in the [interview golden-path guide](../guides/interview-golden-path.md). The live commands require configured OmniRoute, model entitlement, Prime routes and `ocr`; the credential-free quickstart is a separate fixture and does not prove live-provider availability.

1. **Name the evidence boundary.** Open the [certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md). State that the branch was fresh-clone tested, not merged to `main`, and the faults below are tagged injections against real route capacity.
2. **Show eligibility.** Run `verdict eligibility --scope cc/,cx/ --probe --frontier`. Discuss the order of checks; do not claim that a listed catalog entry alone is available.
3. **Show the goal path.** In a disposable repository with the guide's prerequisites, run `verdict orchestrate "<goal>" --repo /path/to/repo --scope cc/,cx/ --max-parallel 3`. Show planning, separate worker worktrees, validation and integrated review. The recorded demonstration is `certlive` in `~/.verdict/evidence/golden-path/certlive-run/`.
4. **Show a documented failure, not a fabricated live outage.** Inspect `~/.verdict/evidence/golden-path/live9-run/receipt.json` for model-scoped quota and reassignment, or `live12-run/receipt.json` for a provider-scoped Codex quota and reassignment to Claude. Inspect `live7-run/receipt.json` for the exhausted-pool `BLOCKED` outcome. [Scenario matrix](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).
5. **Verify the verdict.** Run `verdict watch <run-dir> --once` and `verdict run-receipt <run-dir>` against a recorded run directory. Contrast the review gate and receipt with a response that merely says the work was done.

For a deterministic, credential-free entry point, run `verdict quickstart --non-interactive --dry-run`. The [certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) records this gate separately from `certlive`.

## Limits to say out loud

A blocking review finding stops the run as `BLOCKED`; automatic remediation and re-review are **not shipped**. Adaptive concurrency is **not shipped**; `--max-parallel` is operator-set. Neither a main-branch merge nor CI on a merged commit is certified here. The recorded faults demonstrate classifications and recovery branches; they are not estimates of provider outage frequency or service-level reliability. [Certification limits](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md); [ADR-036 limits](../adr/ADR-036-goal-to-receipt-orchestration.md).
