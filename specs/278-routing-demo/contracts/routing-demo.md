# Contract: routing-demo/v1

Portfolio routing demo surface for issue #278. Core remains policy authority via Feature 276 cheaper-first helpers.

## Run request

```json
{
  "schema_version": "routing-demo/v1",
  "mode": "live",
  "gateway_base_url": "http://localhost:20128/v1",
  "request_count": 100,
  "recorded_path": null
}
```

- Default `mode=live` fetches catalog+pricing from `gateway_base_url`.
- `mode=recorded` requires `recorded_path` pointing at a prior real capture; output must label recorded.
- `request_count` MUST be 100 for the portfolio demo.

## Summary response (stdout JSON or printed sections)

```json
{
  "schema_version": "routing-demo/v1",
  "mode": "live",
  "status": "completed",
  "request_count": 100,
  "routed_cost_usd": 0.0,
  "baseline_cost_usd": 0.0,
  "savings_usd": 0.0,
  "savings_pct": 0.0,
  "success_rate": 0.0,
  "avg_latency_ms": 0.0,
  "wall_clock_ms": 0,
  "catalog_captured_at": "ISO-8601",
  "baseline_definition": "costliest qualified kept identity per request",
  "decisions": [
    {
      "request_id": "r001",
      "class": "simple",
      "chosen_id": "provider/model",
      "baseline_id": "provider/expensive",
      "rationale": "cheaper-first among qualified kept",
      "routed_cost_usd": 0.0,
      "baseline_cost_usd": 0.0,
      "latency_ms": 12.0,
      "success": true
    }
  ]
}
```

## Blocked response

```json
{
  "schema_version": "routing-demo/v1",
  "mode": "live",
  "status": "blocked",
  "reason": "live_surface_blocked",
  "request_count": 0
}
```

Process exit MUST be non-zero on `blocked` for the default live entrypoint.

## Forbidden

- Emitting `status=completed` savings from an invented fixture catalog presented as live.
- Selecting opaque `auto/*` identities as `chosen_id`.
- Including secrets, credentials, raw prompts, or completions in the response.
