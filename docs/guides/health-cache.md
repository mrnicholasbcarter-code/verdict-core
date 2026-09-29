# Health cache and the prove-at-rest prober

`verdict prove-at-rest` probes admitted OmniRoute routes and records the
result in one health cache. The selection ladder reads this cache to apply
free-first worker ordering and the agentic gate.

## Free-first worker order

Implementation workers use the capacity order:
**FREE** (with agentic probe PASS) → **SUBSCRIPTION** → **METERED**.
UNKNOWN is never used for implementation unless explicitly opted in
(set `VERDICT_ALLOW_UNKNOWN_CAPACITY=1` or pass `allow_unknown_capacity=True`).

Planning, controller, and independent review tasks use:
**SUBSCRIPTION** → **FREE** → **METERED** → **UNKNOWN**.

A FREE route qualifies as an implementation worker **only** when a fresh
AGENTIC probe PASS is in the health cache. A single-call tool PASS alone
qualifies it for chat or summary roles (frontier-worthy tasks).

Without a health cache attached, FREE routes are **not** implementation-eligible
(`no_health_cache`); selection falls through to SUBSCRIPTION / METERED.

Free-first ordering applies to the **eligibility ladder** when a health
cache is attached. `verdict orchestrate` does not use this path yet;
the live proof will land in a follow-up.

## Probe classes

| Class | Turns | Qualifies for | Default interval |
|---|---|---|---|
| `single_call` | 2 (chat + one tool call) | Chat, summary, liveness | Every cycle |
| `agentic` | 3 (read file, edit file, confirm read) | Implementation workers | Once per model per 24 h |

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
| `probe_class` | `agentic` or `single_call` |
| `agentic_ok` | True only when a 3-turn agentic probe passed |
| `agentic_checked_at` | When the last agentic probe ran (separate from `checked_at`) |
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

### Receipts

Each selection records per route:

| Field | Source |
|---|---|
| `capacity_class` | Economic class of the selected route |
| `probe_class` | Which probe qualified the route (agentic / single_call / none) |
| `cache_checked_at` | ISO-8601 timestamp of the probe that qualified it |
| `cache_freshness` | `fresh`, `stale`, or `None` if no cache entry |

These fields appear in `rank_components` and in `verdict routing` output.

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
6. **Agentic probes** for FREE routes that need them (up to 8 per cycle).

### Single-call probe

Two steps: chat `Reply with exactly: OK`, then one required tool call
(`verdict_probe_ping`). `tool_ok` means the tool call succeeded.

### Agentic probe

Three turns with simulated file tools:

1. Read a file (`verdict_probe_read_file`).
2. Apply a one-line edit (`verdict_probe_edit_file`).
3. Read the file again to confirm.

Scored pass/fail. A route with agentic PASS sets `agentic_ok=True` in the
cache entry. This is the gate for FREE routes to serve as implementation
workers.

The agentic probe runs once per model per 24 hours (default). It uses the
same bounds, redaction, and atomic writes as the single-call probe.

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
uv run pytest tests/test_health_cache.py tests/test_prove_at_rest.py tests/test_free_first_order.py -q
```
