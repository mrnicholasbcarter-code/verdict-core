# Feature Specification: Worthiness gate (BOD-107)

## Issue

Linear BOD-107. Parent BOD-97.

## Goal

Classify each task `worthy` vs `ordinary` with named `class_reasons` on the
receipt **before** free-first ranking. Worthy work may only select frontier /
high-cap models. Ordinary work is free-first then lesser-paid (BOD-109).

## Functional requirements

1. Classifier emits `task_class` (`worthy`|`ordinary`) and `class_reasons[]`.
2. Rules are explicit (escalation labels, architecture/security phrases, explicit envelope `task_class`). No invented worthiness score.
3. Worthy path never selects free-tier solely for cost (named drop `worthy_excludes_free_for_cost`).
4. Ordinary path does not burn frontier without a named reason (chooser ordinary ranker).
5. Silent class flip is a QA fail.

## Proof

- Hard architecture/security task → worthy → frontier-class model.
- Ordinary coding/summarize → ordinary → free when available.

## Non-goals

- OmniRoute task routing
- Invented worthiness scores
