---
name: verdict-proof
description: Use when a Verdict worker returns implementation evidence or before declaring local validation complete or opening a PR.
---

# Verdict proof

Read `docs/guides/prime-workflow.md` and the hydrated proof requirements. Parent-run
verification is authoritative over the worker's narrative or exit code alone.

1. Validate receipt identity — including `dispatch_id` against the packet `attempt_id`,
   `git_head` against `head_sha`, and `last_progress_at` not preceding `started_at` — confirm the
   ACTIVE lease generation still owns the issue, and compare actual changed files against allowed
   scope. Inspect
   changes and applicable documentation/ADRs using existing review tools. A changed objective,
   acceptance criterion or scope requires rehydration.
2. Run the required STATIC, UNIT, INTEGRATION and ACCEPTANCE-PROOF commands with explicit cwd
   and deadlines. Capture command, exit code, source SHA, raw output, artifact path and digest.
   Baseline repo tools are pytest, ruff, strict mypy, and packaging checks as applicable;
   TypeScript contract changes also require the existing contract tests/parity validation.
   Do not replace integration with unit tests or claim a dry-run as live acceptance.
3. Every AC maps to observed evidence. Missing tools, timed-out checks, empty test discovery,
   skipped mandatory tests and unavailable required live services are BLOCKED, never PASS.
   N/A is allowed only with the predeclared applicability reason and authority from hydration.
   ACCEPTANCE-PROOF always applies, including documentation/discovery behavior.
4. Bind proof to a clean commit of precisely the reviewed changes. If source changed after
   testing, rerun affected verification; never merely overwrite the proof SHA. Store evidence
   outside tracked source in the common-git state directory. Hash artifacts and write proof.json.
5. Run `validate_proof` and verify every artifact/digest. Only complete current-source evidence
   advances LOCAL_VALIDATION -> PROOF_COMPLETE. On failure return to the same owned attempt
   for a bounded fix and repeat validation. Do not open even a draft PR with incomplete proof.
6. Hand proof and source identity to verdict-finish for independent review. The supported PR
   path is `scripts/prime_workflow.py open-pr`; it rechecks proof, review, clean tree and pushed
   source. Never bypass this gate with direct `gh pr create`, HTTP calls or another integration.

The helper enforces local bundle integrity, not semantic truth of fabricated logs. Reviewers
must verify the commands and AC evidence actually demonstrate the claimed behavior.
