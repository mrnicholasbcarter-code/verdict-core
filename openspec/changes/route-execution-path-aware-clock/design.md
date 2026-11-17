# Design

## Context

`IntelligenceService._prepare_execution_path_request` evaluates execution-path candidates by calling several admission functions: `gate_capability`, `build_selector` + `selector._assess_all`, `gate_admit_prove_confirm`, `apply_best_of_admitted`, and `lookup_omniroute_id`. Each of these validates that the `now` parameter is a timezone-aware `datetime` via `health_cache._aware`. The service's `admit_now` field defaults to `None` and was passed directly without a fallback, unlike other call sites in the same file that use `self.admit_now or datetime.now(timezone.utc)`.

## Goals / Non-Goals

**Goals:**
- Resolve a single timezone-aware `now` per request and reuse it at every call site in the execution-path preparation flow.
- Preserve an explicit test-injected `admit_now` when provided.
- Ensure `POST /v1/route` with `execution_path_request` returns a non-500 status (200 or 4xx).

**Non-Goals:**
- Change the execution-path optimizer (`optimize_execution_path`) — it already uses a timezone-aware fallback.
- Change any API contract, gate, dependency, or install behavior.
- Add a new configuration option.

## Decisions

Add a read-only `_now` property to `IntelligenceService`:

```python
@property
def _now(self) -> datetime:
    from datetime import datetime, timezone
    return self.admit_now if self.admit_now is not None else datetime.now(timezone.utc)
```

Update all nine call sites in `intelligence.py` to use `self._now` instead of `self.admit_now`:

1. `lookup_omniroute_id(..., now=self._now)` — line ~1042
2. `build_outcome_shadow_index(..., as_of=self._now)` — line ~1222
3. `gate_capability(..., now=self._now)` — line ~1281
4. `build_selector(..., now=self._now)` — line ~1311
5. `selector._assess_all(orch_reqs, self._now)` — line ~1315
6. `gate_admit_prove_confirm(..., now=self._now)` — line ~1380 (two call sites)
7. `gate_capability(..., now=self._now)` — line ~1654 (legacy admit path)
8. `gate_admit_prove_confirm(..., now=self._now)` — line ~1684 (legacy admit path)
9. `apply_best_of_admitted(..., now=self._now)` — line ~1743

The property is evaluated at each access, but within a single request the wall-clock drift is negligible and acceptable. A cached `_resolved_admit_now` was considered but rejected to keep the implementation simple and avoid stale-time concerns if the service instance is reused across requests.

## Risks / Trade-offs

- Re-evaluating `datetime.now(timezone.utc)` at each call site could theoretically produce different values within one request. The drift is microseconds and does not affect admission decisions. A cached value would add state and complexity for no practical benefit.
- The legacy admit path (lines 1654, 1684, 1743) also uses `self._now`, which is correct because it shares the same service instance and the same request.

## Security / trust implications

A missing clock must never cause a server error that could be confused with a route rejection. The fix ensures a clear 4xx with a named reason when admission fails, and 500 only for genuine server errors.

## Routing / context / memory implications

No routing policy, context pipeline, or memory store changes. The `_now` property is advisory for time-dependent admission only.

## Migration / rollback

No data migration. Reverting the nine call sites to `self.admit_now` restores the previous behavior (500 on execution-path requests without injected time).

## ADR impact

ADR-0001 (control plane invariants) remains governing: unknown is never healthy, opaque routes are never candidates, attempts are bounded. ADR-035 (execution-path authority) remains governing: the optimizer is the sole strategy authority. No ADR file changes.