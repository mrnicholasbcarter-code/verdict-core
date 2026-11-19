# Design: review-findings-lost

## Context

OCR reports every tool call the reviewer made under `tool_calls`. A `code_comment` call whose arguments do not parse is listed in `failure_by_tool` and `failure_details` and its comments never reach `comments`. Verdict only read `comments`.

## Goals / Non-Goals

**Goals:** a review with lost comment calls is never PASS; the loss is visible in events and the receipt; one bounded retry.

**Non-Goals:** parsing or repairing the malformed arguments (that would invent findings); changing provider-failure recovery; treating failed non-comment calls (for example `file_read`) as lost findings.

## Decisions

1. **Signal**: failed `code_comment` calls only. They are the only tool that carries findings. The larger of the two OCR counts is used so a partial report cannot hide a loss.
2. **Status**: ERROR, not FAIL. FAIL means blocking findings exist; here findings are unknown. A parsed blocking finding still makes the review FAIL.
3. **Retry**: same route, once, within `max_reviewer_attempts`. A malformed tool call is a model-output fault, not a provider outage, so there is no cooldown and no reselection, matching the coverage-error rule.
4. **Evidence**: `findings_lost` is added to events and the receipt only when non-zero, so older receipts recompute byte-for-byte.

## Security / trust implications

OCR output stays untrusted input. The malformed arguments are never parsed into findings, so a reviewer cannot inject findings through a broken call, and a dropped call can no longer hide findings behind a PASS.

## Routing / context / memory implications

None for routing: a lost-findings rerun keeps the same admitted reviewer and records no cooldown. No context-pack or memory change.

## Risks / Trade-offs

- A reviewer that keeps emitting malformed calls blocks the run. That is the fail-closed choice the invariant requires.

## Migration / rollback

Additive. Revert the commit to roll back.

## ADR impact

None. Applies ADR 0001's fail-closed review invariant.
