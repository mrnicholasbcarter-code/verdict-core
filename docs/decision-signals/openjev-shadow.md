# OpenJev / Codiv System-One Decision Signals (SHADOW and ADVISORY modes)

## Overview

Verdict can send a scrubbed, truncated description of each task to
[Codiv](https://codiv.ai/) (the public OpenJev API) and receive typed
probability signals — *complexity*, *frontier_worthy*, *security_sensitive*,
and four others — before the planning step runs.

Two opt-in modes are supported in v0.3.0:

| Mode | When signals are collected | Effect on routing |
|------|---------------------------|-------------------|
| **SHADOW** | Once per `verdict orchestrate` run, before planning, **for every goal** (no per-task privacy filter) | None — recorded in EventLog only |
| **ADVISORY** | Once per route call for non-protected, non-restricted tasks | Read-only by the intelligence layer; routing is never blocked |

> **SHADOW scope**: when SHADOW is active the scrubbed goal is sent for **all** orchestrate
> runs. There is no per-task protected/restricted filter at the SHADOW call site. See
> `PRIVACY_POLICY.md` §"SHADOW scope note" for mitigations.

Both modes are **off by default**. Routing outcomes are identical whether
the provider is healthy, failing, or absent.

---

## Feature flag

```bash
export VERDICT_DECISION_SIGNALS_MODE=SHADOW    # or ADVISORY
```

Allowed values: `OFF` (default), `SHADOW`, `ADVISORY`.
Any other value is treated as `OFF` with a warning.

---

## Configuration

```bash
# Official TypeSafe / Codiv SDK variable names (required)
export TYPESAFE_API_KEY="sk-codiv-..."
export TYPESAFE_BASE_URL="https://api.codiv.ai"   # default; can be omitted

# Optional: override the pinned model (default: openjev-0.1)
export VERDICT_OPENJEV_MODEL="openjev-0.1"
```

If `TYPESAFE_API_KEY` is empty or `VERDICT_DECISION_SIGNALS_MODE` is `OFF`,
no network call is made. The `OPENJEV_API_KEY` and `OPENJEV_BASE_URL`
variables from the original decision signal contracts prototype are no longer read.

---

## What is sent

For every signal collection call the provider sends one HTTPS POST to
`TYPESAFE_BASE_URL/v1/systemone` with:

```json
{
  "model": "openjev-0.1",
  "state": "<scrubbed, truncated goal — max 500 chars>",
  "questions": { ... fixed question set ... }
}
```

The `state` field is:

* The task purpose label plus the task summary, truncated to 500 characters.
* Passed through `verdict.orchestration.receipt.scrub_secrets` before
  transmission — patterns that look like API keys, tokens, passwords, or
  connection strings are replaced with `[REDACTED]`.
* **Never** includes file contents, repository paths, or credential values.
* **Never** sent when the mode is `OFF` or when `TYPESAFE_API_KEY` is absent.

The fixed question set asks seven typed questions (noul / score / choice)
about the task. See `verdict/decision_signals/openjev.py` (`_QUESTIONS`)
for the exact definitions.

---

## What is received and recorded

A successful 200 response returns `{model, answers, usage}`. The provider
maps the answers to `DecisionSignalSetV1` signals in `[0,1]`:

| Signal | Type | Mapping |
|--------|------|---------|
| `complexity` | score (4 levels) | `score / 3` |
| `decomposability` | score (3 levels) | `score / 2` |
| `ambiguity` | score (3 levels) | `score / 2` |
| `frontier_worthy` | noul | probability of yes |
| `security_sensitive` | noul | probability of yes |
| `verification_strength` | score (3 levels) | `score / 2` |
| `context_need` | score (3 levels) | `score / 2` |

The signal set is written to the EventLog as a `decision_signals` event and
is included in the receipt under `decision_signals`. It does **not** alter
the planning result or any routing decision in SHADOW mode.

In ADVISORY mode the intelligence layer may *read* the signals when selecting
a route, but routing is never blocked on a signal failure.

---

## Data-flow diagram

```
verdict orchestrate (or golden path)
  │
  ├─ factory.provider_from_env()
  │    reads VERDICT_DECISION_SIGNALS_MODE + TYPESAFE_API_KEY
  │    returns None if OFF or key absent
  │
  └─ plan_with_failover()
       │
       ├─ build state: scrub_secrets(goal[:500]) + numeric hints
       ├─ POST /v1/systemone  ──────────────────────► api.codiv.ai
       │    {model, state, questions}                  (HTTPS, TLS)
       │                      ◄────────────────────── {model, answers, usage}
       │
       ├─ map answers → DecisionSignalSetV1 signals [0,1]
       ├─ emit EventLog event type="decision_signals"
       └─ (signals have NO effect on WorkGraph or routing)
```

ADVISORY adds a second call site inside the intelligence layer (per route
call, only for tasks that are not privacy-restricted or marked trusted_upstream).

---

## Failure handling

Failures never block planning. The signal set records `failure_class`:

| Condition | failure_class |
|-----------|---------------|
| Key absent | UNKNOWN |
| Network error | TRANSPORT |
| Timeout | TIMEOUT |
| 401 / 403 authentication_error | AUTHENTICATION |
| 403 permission_error | AUTHORIZATION |
| 429 quota_exceeded_error | QUOTA (not retried) |
| 429 rate_limit_error | RATE_LIMIT + retry-after |
| 529 overloaded_error | OVERLOADED + retry-after |
| 400 / 422 | INVALID_REQUEST |

---

## Rollback

```bash
export VERDICT_DECISION_SIGNALS_MODE=OFF
# or unset the variable
```

No provider calls, no EventLog events, no receipt extension.

---

## Timeout

The provider uses `VERDICT_DECISION_SIGNALS_TIMEOUT_MS` (default **1500 ms**) for the
Codiv API call. This runs **before** planning, so a slow call adds directly to the
orchestrate wall time.

> **Cold-start note**: the 1.5 s default may be too tight when the Codiv API is
> warming up (first call of the day, cold TLS handshake, etc.). A live run recorded
> 1515 ms latency against the 1500 ms cap, which correctly produced a `TIMEOUT`
> failure and allowed planning to continue unaffected.
>
> If you see frequent `failure_class: timeout` in your EventLog while Codiv is
> otherwise healthy, raise the limit:
> ```bash
> export VERDICT_DECISION_SIGNALS_TIMEOUT_MS=5000 # 5 s
> ```
> OpenJev ADVISORY mode ([advisory.md](advisory.md)) uses the same env var.


## Running the smoke test

```bash
export TYPESAFE_API_KEY="sk-codiv-..."
python scripts/smoke_openjev.py
# last line: RESULT: PASS
```

Exit 0 = success, 1 = call failed, 2 = key missing.
