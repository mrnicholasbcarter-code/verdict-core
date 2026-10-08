# Design: Review coverage admission and integration command evidence

## Context

See `proposal.md` for the motivation and BOD-288 scope. At the source base `dedd74d513691b9b11898a20506d4b013f9771e3`, `OpenCodeReviewer._interpret` in `verdict/orchestration/review.py` checks subprocess/output validity, then `_empty_review_reason`, every-item failure, selected reviewer identity, and blocking findings. `_empty_review_reason` rejects explicit skipped status/terminal state, `manifest.coverage.selected == []`, and invalid or nonpositive `summary.files_reviewed`; absent fields return no error. As a result, metadata-free objects can reach the findings check and PASS.

`OpenCodeReviewer.review` only reselects after ERROR when `_provider_failure` recognizes the error detail. Existing skipped/coverage error strings do not match its provider-failure tokens. The exact new detail must follow this same non-provider path.

`DagRuntime._integrate_node` in `verdict/orchestration/runtime.py` already uses `_resolve_verify_argv` before invoking its runner. Its verify event joins original arguments with spaces and omits the resolved command. The worker `_validate` path already emits `shlex.join` for both commands, plus optional `resolved_argv0`. Receipt verification in `verdict/orchestration/receipt.py` checks the retained event-log digest and rebuilds the receipt from recorded events, rather than re-running commands or passing old OCR files through `_interpret`.

**KNOWN** ground truth, read in full as JSON:

| Retained complete output | Observed coverage and outcome inputs |
| --- | --- |
| `docs/proof/dogfood-bod-273-2026-09-28/review/ocr-raw.json` | OCR v1.12.9; status and terminal state `complete`; model `codex/gpt-6-sol`; selected/completed lengths 1/1; `files_reviewed` 1; comments empty; SHA-256 `e0cf618b6fb15eb693113d95cf367dcd44be6c279dcd1ee9285fba9ba60c6f8a`. |
| `/home/nick/.verdict/evidence/final-6254a59/runs/clean/20261008T041611Z/review/ocr-raw.json` | OCR v1.12.9; status and terminal state `complete`; model `cx/gpt-5.5`; selected/completed lengths 2/2; `files_reviewed` 2; comments empty; SHA-256 `4bc3938ceed72e231eed7dc98d8ae08ceec6d86febe9c7818b124e0fc546450e`. |

Both use real `comments`, `groups`, `llm`, `summary`, and `ocr.run-manifest/v1` data rather than a synthetic findings-only success shape. Their inspection proves these shapes carry positive evidence; it is not new certification evidence.

## Goals / Non-Goals

**Goals:** Make OCR interpretation fail closed for coverage-free objects; retain all existing negative checks and findings decisions; keep coverage errors outside reviewer-provider recovery; and give integration verify events the same argv evidence as worker events. Keep tests portable and allow two implementation units to run independently.

**Non-Goals:** Do not validate the entire OCR schema, require selected/completed sets to match, prove every changed file was reviewed, sign review evidence, change provider eligibility/recovery, change the shared command resolver, migrate receipt schemas, or retroactively reject historical receipts. Do not change public wording or certification records in this work. The spec author changes only this OpenSpec directory; subsequent implementation is limited to the four files in the task ownership contracts.

## Decisions

Use an additive coverage guard at the existing interpretation boundary, leave coverage errors outside provider recovery, and mirror worker command evidence at integration. Preserve historical event replay rather than migrate receipts. The four decisions below define precedence, compatibility, and the disjoint test ownership.

### 1. Preserve explicit vetoes, then require one positive signal

Keep `_empty_review_reason` as the coverage decision boundary so `_interpret` remains the single consumer. Apply these decisions in order:

1. Preserve the existing explicit vetoes and their details: skipped status or terminal state gives `review skipped: no items reviewed`; an explicitly empty selected list gives `review skipped: no items selected`; an unparseable file count gives `review coverage invalid: files_reviewed is not a count`; a parsed count at or below zero gives `review skipped: zero files reviewed`.
2. If none of those vetoes applies, recognize positive evidence only when `manifest.coverage.selected` is a non-empty **list**, OR `manifest.coverage.completed` is a non-empty **list**, OR `summary.files_reviewed` parses by the existing count conversion and is greater than zero. Mapping guards prevent malformed manifest/coverage/summary containers from raising. Preserve the existing count conversion and invalid-count behavior; a broader count-type/schema tightening is not part of this change.
3. If none of the three signals is positive, return exact detail `review coverage missing`. Absent coverage or non-list values cannot satisfy a list predicate. They produce this ERROR when no alternate positive signal exists. A valid positive summary alone is sufficient, even without a manifest, because the acceptance contract explicitly permits that alternative.
4. After coverage admission, preserve the existing every-item-failed, reviewer-identity, and blocking-finding decisions. Positive coverage is necessary for PASS, not sufficient to override another error or a blocking finding.

Examples resolve the precedence: `{}` and `{"findings": []}` fail; `{"summary": {"files_reviewed": 1}, "findings": []}` can pass; `selected: []` with a positive completed list still fails; a positive selected list with `files_reviewed: 0` still fails. A completed-only payload must omit selected rather than explicitly set it empty to avoid the existing veto.

Rationale: the additive positive-evidence guard closes the identified fail-open path without relaxing explicit failures or requiring only the newest complete manifest schema. Rejecting every payload without a manifest would wrongly discard summary-only evidence. Treating any truthy value as coverage would allow strings, mappings, and booleans to masquerade as item lists. Requiring all three positive fields or complete selected/completed equality would exceed the requested contract and need separate producer compatibility work.

### 2. Coverage errors are terminal input errors, not provider outages

The new ERROR detail is exactly `review coverage missing`, without timeout/provider/exit tokens. `_provider_failure` must remain false for this result, as it is for skipped review. Return the first reviewer result without selecting another route, adding a cooldown, or claiming pool exhaustion. A different model cannot repair missing coverage evidence in the supplied output contract. Genuine timeout, nonzero OCR exit, identity substitution, or every-item-failed recovery remains unchanged.

Alternative rejected: retry or reselect on every ERROR. That would hide coverage-contract failures behind provider recovery and consume another review attempt without a justified outage signal.

### 3. Mirror the worker event contract at integration

In `_integrate_node`, keep resolving and running the same argv as today. Emit `command = shlex.join(node.verification_command)` and `executed_command = shlex.join(resolved)`. Emit both on successful and failed verification. Preserve `ok`, `exit_code`, `tail`, node identity, and optional `resolved_argv0`. Do not alter the worker path or the no-command integration behavior.

Rationale: `shlex.join` makes spaces and shell-sensitive characters auditable without confusing a multiword argument with several arguments. The executed value records the argv actually passed to the runner, including interpreter resolution. Merely adding the executed field while retaining space-joining would leave integration evidence inconsistent with worker evidence. A shared event-builder refactor is unnecessary and would expand the ownership surface.

### 4. Keep replay compatibility and hermetic tests within the owned files

Do not modify `receipt.py`. Old events may lack `executed_command` and may contain the old raw command string; preserve them verbatim during replay. `verify_run_receipt` must continue to use their recorded bytes and existing reconstruction rules. New fields are additive event evidence, not a new required receipt field. Do not open historical raw OCR output to apply the new coverage rule during receipt verification.

Unit A owns only `verdict/orchestration/review.py` and `tests/test_orch_review.py`. It must test both full real payload shapes, the three isolated positive signals, explicit-veto conflicts, minimal/malformed payloads, and no provider reselection. Read the repository proof payload in the test using a repository-relative path. Freeze a complete copy of the inspected Interview Clean payload inline in `tests/test_orch_review.py`, preserving its real manifest/summary/comments shape; do not introduce a fixture file or require the developer-home evidence path. Interpret the complete samples with their recorded model route, or use the existing fake runner's route identity handling, so an unrelated identity mismatch cannot mask coverage compatibility. Keep negative payloads unaugmented: test helpers must not automatically add missing coverage.

Unit B owns only `verdict/orchestration/runtime.py` and `tests/test_orch_runtime.py`. Use fake/injected runner and resolver boundaries to check original versus resolved argv, unchanged argv, quoting-sensitive arguments, successful and failed verify events, and retained optional resolution metadata. Add receipt compatibility coverage in that same test file using the existing receipt APIs: write and verify legacy-shaped events without `executed_command`, with the old command rendering, and an ERROR-free historical recorded review PASS; do not re-interpret raw OCR. Leave `tests/test_orch_receipt.py` read-only and include it in the final integration check.

Verification commands, from the repository with its lockfile-synced environment active, are `python -m pytest -q tests/test_orch_review.py` for A, `python -m pytest -q tests/test_orch_runtime.py` for B, and `python -m pytest -q tests/test_orch_review.py tests/test_orch_runtime.py tests/test_orch_receipt.py` after both. No live model/provider/OmniRoute call or full-suite run is required.

## Risks / Trade-offs

- **Metadata-free producer compatibility** → Such output deliberately changes from potential PASS to ERROR. The two inspected complete v1.12.9 shapes and existing clean/findings fixtures carry positive evidence. Test all three positive alternatives rather than assume every producer has all fields.
- **Conflicting positive and negative metadata** → Explicit existing vetoes win. Regression tests must make each veto compete with positive evidence so the new OR rule does not relax existing fail-closed behavior.
- **Selected-only evidence is weaker than full completed coverage** → The requested contract accepts a non-empty selected list. Preserve every-item-failed rejection and do not market this as proof of complete or correct semantic coverage.
- **Portable retained external payload** → An absolute developer-home file is available now but not in worker/CI environments. Freeze its complete shape inline in the owned review test file; never require that path at test runtime.
- **Historical command formatting drift** → Changing the replay projection or re-rendering past commands could break receipts. Change only future integration event production and exercise legacy receipt reconstruction without editing receipt code.
- **Wider producer/test or wording changes** → No other file is expected to need edits. Existing demo OCR output also contains non-empty selected/completed lists, and the core README's narrowed claims remain true. The separate profile text is **NOT AVAILABLE** for independent inspection here. If implementation reveals an incompatible producer or a necessary public wording correction, report it and re-scope separately; do not edit outside the four owned files.
- **Certification SHA binding** → Any later `verdict/` edit invalidates rehearsals tied to the previous certified SHA. Retain the current certification evidence before dispatching implementation, and require new certification evidence separately before any updated certification claim. This artifact-only commit does not assert that retention or recertification occurred.

## Security / trust implications

OCR output is untrusted evidence. Findings absence is not coverage evidence, and a truthy non-list field is not an item list. Safe container/type checks must return a coverage error instead of raising or permitting a metadata-free PASS. Positive metadata still does not authenticate the output or prove semantic correctness; existing selected-model identity and blocking-findings checks remain in force.

`shlex.join` is evidence formatting only: the runner still receives resolved argv as a sequence, not a newly parsed shell string. Do not introduce shell evaluation, extra command execution, environment expansion, or new credential capture. Logging the executed argv has the same data exposure class as worker verify events; this change does not add secret redaction or claim that arbitrary verification arguments are secret-free. Receipt integrity remains a digest against the retained log/receipt, not a signature against replacing both.

## Routing / context / memory implications

Model eligibility, reviewer exclusions, route/family independence, and genuine provider recovery are unchanged. A coverage ERROR exits after one reviewer launch and must not cool or reselect routes. Worker transcript hydration, planner prompts, context packing, and persistent memory are not touched.

OpenSpec is the implementation contract, not a new routing or certification authority. Bound orchestration must use the exact change slug and its revision digest; changed artifacts require a new/replanned run under the existing lifecycle. Recorded coverage failures remain evidence of that run, not memory that penalizes a provider across runs.

## Migration / rollback

No data or receipt-schema migration is needed. Retain existing certification evidence first; then implement the two independent file-pair units and run their focused checks plus the final three-file check. Historical events and proof bundles remain untouched and verify under the current replay contract. Only newly interpreted coverage-free OCR output changes to ERROR; newly emitted integration events gain the executed argv and correctly quoted original argv.

Rollback reverts the four implementation files on a later code branch, with no rewriting of recorded logs or receipts. It restores the known fail-open coverage gap, so rollback must be explicit and must not carry a fail-closed claim. Additive executed-command fields in retained new logs require no rollback migration. Strict OpenSpec validation and local admission do not replace independent implementation review, exact-head CI, merged-main checks, or new certification rehearsals.

## ADR impact

This strengthens the existing independent-review and event-evidence boundaries in ADR-036 (`docs/adr/ADR-036-goal-to-receipt-orchestration.md`): a coverage-free result is not review evidence, and verification evidence describes actual execution. It introduces no new orchestration component, selector authority, retry owner, or receipt format. No new ADR or edit to the existing ADR is required for this bounded change; a future design that mandates full completed coverage or authenticated review output would need separate architectural consideration.
