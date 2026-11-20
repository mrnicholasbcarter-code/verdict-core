# Design — discovery-refresh-timer

## Context

`$VERDICT_HOME/discovery/registry.json` stores tool-probe results for
FREE-capacity routes with a 12-hour TTL (`REGISTRY_TTL_S = 43200`).
`EligibilityLadder._task_gate` (S3b) consults this registry when
`capacity == FREE` and no fresh agentic probe exists.  Without a background
timer the registry expires unattended, `load_registry()` returns `None`
(unknown), and free routes fail the tool-proof gate even when they are
genuinely capable.

## Goals / Non-Goals

**Goal**: a background timer that calls `verdict discovery refresh --free-only
--force` every 6 hours so the registry is always within the 12-hour TTL, with
no paid or subscription model calls.

**Goal**: the timer probes FREE-capacity routes only (`--free-only`); no
subscription or paid routes are probed.

**Non-goal**: keeping subscription entries in the registry (they can be
refreshed by running `verdict discovery refresh` manually without `--free-only`).

## --force justification

Without `--force`, `run_refresh_action` skips when `load_registry()` returns a
non-None result (i.e. the registry is within TTL).  A timer at 6h would fire at
T=6h but skip because the registry written at T=0 is still within its 12h TTL.
The effective re-probe interval would be 12h — right at the TTL boundary, with
no safety margin.  With `--force`, every timer fire produces a fresh probe pass,
keeping entries no older than ≈6h.

## Interval justification

`INTERVAL_S = REGISTRY_TTL_S // 2 = 21_600 s = 6 h`.  One missed timer fire
leaves entries ≈12h old — exactly at the TTL boundary — and the subsequent fire
at ≈18h re-probes because `load_registry` returns `None` on expiry.

## --free-only overwrite note

A `--free-only` run writes a registry containing only free entries, dropping
any subscription entries from a prior manual full refresh.  This is acceptable
because `_task_gate` only consults the registry when `capacity == FREE` (S3b),
so subscription entries are not load-bearing for the admission path the timer
exists to serve.
