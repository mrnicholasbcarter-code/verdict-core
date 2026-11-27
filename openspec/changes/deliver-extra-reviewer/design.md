# Design — bounding and choosing the ready gate's extra review

## Context

`make_cross_family_reviewer` builds a selector over the live ladder with no scope and no preference and hands it to `OpenCodeReviewer`, excluding the implementer families. The reviewer tool is run with an effort level and an aggregate token budget scaled from the size of the changed files. It is not given `--max-tokens`, the ceiling on one group's prompt, so a reviewer that reads large files repeatedly can spend the whole budget on part of the diff.

## Goals / Non-Goals

Goals:

- The extra review finishes on diffs that touch large modules.
- The operator can say which admitted route should review, without Verdict naming a model in code.
- The gate's decisions and its fail-closed behaviour stay as they are.

Non-Goals:

- Making the extra review a strong check. On the evidence so far no free route found the defects a stronger reviewer found; this change is about finishing.
- Changing the in-run review.
- Exempting any route from quota or health gates (the Codex routes are held back as `quota_low`; they stay held back).
- Retrying on a second route after an incomplete review.

## Decisions

1. **A prompt ceiling on the extra review, default 200,000.** `OpenCodeReviewer` gains an optional ceiling and adds `--max-tokens` to the reviewer command only when it is set. Delivery sets it; orchestrate does not. 200,000 is the value the controller's own review script has used throughout and one of the two values tested; 60,000 also finished and found more on one diff, less evidence than is needed to prefer it.
2. **Preference, not a pinned model.** No model name goes into the code. The operator lists preferred routes; the gate builds its selector restricted to those routes (`route_ids`), which still runs every admission and eligibility stage, and asks for a reviewer outside the implementer families. If that yields no reviewer it builds the unrestricted selector, as today. This costs one more ladder build when the preference cannot be met.
3. **A preferred route in an implementer family is ignored, not an error.** The family rule decides; the preference never overrides it.
4. **What reviewed is already recorded.** `extra_review_route` and the status and counts are written to `delivery.json` since #1385.

## Risks / Trade-offs

- A ceiling may make a review shallower. That is not measured.
- A preferred route that is weak makes the gate pass more easily. The gate is one of three reviews of a delivered change (in-run, extra, and the controller's before merge); it should not be the only one.
- Two ladder builds add a few seconds and a few health probes when the preference fails.

## Security / trust implications

The preference comes from the operator's command line or environment. It cannot admit a route, bypass a health, quota or policy gate, or select a route in an implementer family. The reviewer's output is treated as before: an incomplete or unidentified review is an error.

## Routing / context / memory implications

Routing: among admitted reviewer routes the operator's preference comes first; otherwise cheapest-first as today. Context: the reviewer's per-group prompt is capped. Nothing is written to memory.

## Migration / rollback

No stored state changes. With no preference and `--review-max-tokens 0` the behaviour is exactly today's.

## ADR impact

None. ADR-049 (cost policy) still governs which routes are admitted.
