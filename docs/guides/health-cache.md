# Health cache and the prove-at-rest prober

`verdict prove-at-rest` probes admitted OmniRoute routes and records the
result in one health cache. The selection ladder does not read this cache
yet. Selection order is unchanged by this command.

## Cache

Default path: `~/.verdict/health-cache.json`. Override with `--state-path`
or `VERDICT_HEALTH_CACHE`.

One JSON file, one `fcntl` lock, atomic replace. Each route entry has:

| Field | Meaning |
|---|---|
| `state` (derived) | `fresh`, `stale`, or `negative` |
| `category` | Probe outcome (`ok`, `rate_limited`, `timeout`, ...) |
| `checked_at` | When the probe ran |
| `until` | When a negative becomes half-open, or the end of the stale window |
| `consecutive_failures` | Failures in a row; reset on success |
| `chat_ok` | The chat step returned exactly `OK` |
| `tool_ok` | The required tool call succeeded. This is a coding worker. |
| `latency_ms` | Probe latency |
| `pool` | Optional shared-quota pool |
| `capacity_evidence` | Optional capacity label from admission |

A route with no entry is `unprobed`. `unprobed` is never healthy.

### TTLs

| Result | How long |
|---|---|
| Healthy (chat OK; tool OK for a coding worker) | 10 min fresh, usable as stale until 30 min |
| 429 | `Retry-After`, otherwise 60 s |
| 5xx or timeout | 60 s, doubling each consecutive failure, cap 15 min |
| 401, 402, 403, 404, 410, catalog_stale | 6 h, doubling to 24 h |
| Half-open success | The next backoff is half the previous one |

### Read API

`verdict.orchestration.health_cache.HealthCache`:

- `lookup(route, now)` returns the state and the stored entry.
- `healthy_routes(now, predicate)` returns fresh or stale entries that pass
  `predicate`. A coding worker is `predicate=lambda entry: entry.tool_ok`.
- `record(route, result, now)` stores one probe.
- `consume(provider, now)` decrements that provider's token bucket. A 429
  zeroes the bucket until `Retry-After`.

Callers must pass `now`. Nothing in this module reads the clock itself
except the CLI status command.

### Buckets

One sliding window per provider, or per `provider/pool` when a pool is set.
The default is 10 requests per 60 s. Override the capacity per provider by
constructing `HealthCache(..., bucket_overrides={"nvidia": 2})`.

## Prober

`verdict prove-at-rest once` and `daemon` load the admitted set the same way
`verdict eligibility` does: live inventory, live connections, then `admit`.
Only admitted routes are probed.

Each cycle is capped at `--max-requests` (default 300, counting chat and tool
calls) and `--max-wall-seconds` (default 600). Concurrency of the reservation
batch is 4. Partial progress is written after every probe, so a crash keeps
what finished.

Order inside a cycle:

1. Half-open negatives (a failure whose `until` has passed).
2. Stale healthy entries.
3. Never-probed FREE routes, round-robin by provider/pool.
4. SUBSCRIPTION, METERED, and UNKNOWN, chat liveness only.
5. Up to 2 routes from providers that have no fresh entry.

A coding-worker probe is two steps: chat `Reply with exactly: OK`, then one
required tool call (`verdict_probe_ping`). `tool_ok` means the tool call
succeeded. Other capacity classes stop after chat.

The prober does **not** write `~/.verdict/orchestration-health.json`.

## Legacy state

`~/.verdict/prove-at-rest/state.json` (override
`VERDICT_PROVE_AT_REST_STATE`) is the old cycle document. This prober ignores
it: it does not read it, migrate it, or delete it. `load_healthy_passports`
still reads it for callers that have not moved.

## Status

```bash
verdict prove-at-rest status
verdict prove-at-rest status --json
```

Prints counts by state, the top healthy coding workers (lowest latency
first), and providers that have no fresh or stale entry.

## systemd

The unit template is `deploy/systemd/verdict-health-cache.service`. It is a
long-running service with `--interval 600`. Install notes are in
[harnesses.md](harnesses.md#health-cache-prober). The unit is not enabled by
the test suite or by installing the package.

## Tests

```bash
uv run pytest tests/test_health_cache.py tests/test_prove_at_rest.py -q
```
