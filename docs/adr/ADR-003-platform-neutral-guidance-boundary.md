# ADR-003: Platform-Neutral Guidance Boundary

- **Status:** proposed for issue #107
- **Date:** 2026-07-27
- **Deciders:** Verdict Core maintainers
- **Related:** [#107](https://github.com/mrnicholasbcarter-code/verdict-core/issues/107), [#108](https://github.com/mrnicholasbcarter-code/verdict-core/issues/108)

## Context

Verdict Core is used from multiple execution hosts. Project guidance must not
be coupled to Codex, Claude Code, Pi, or any other host, and an optional
guidance experiment must not make the normal routing API depend on local
Ruflo, RuVector, memory, or provider state.

The experimental implementation captured in issue #107 currently has three
problems at its boundary:

1. API startup initializes guidance unconditionally and reads host-specific
   local files.
2. Guidance is exposed as an implementation detail instead of one explicit,
   versioned contract.
3. The local guidance package is treated as a runtime dependency even though
   the feature is experimental and optional.

## Decision

Keep guidance as an optional, in-process Verdict feature behind an explicit
configuration boundary. The boundary is host-neutral and has these rules:

- Guidance is disabled by default. Normal API startup must not import, load,
  or initialize guidance state.
- `VERDICT_GUIDANCE_ENABLED=1` explicitly enables the feature. The optional
  `VERDICT_GUIDANCE_PATH` and `VERDICT_GUIDANCE_LOCAL_PATH` variables identify
  the constitution and local overlay. Paths must resolve inside the configured
  repository root.
- When enabled, initialization is bounded by
  `VERDICT_GUIDANCE_INIT_TIMEOUT_MS` and reports `ready`, `degraded`, or
  `disabled` status. Missing or malformed guidance is actionable degraded
  state, not an API-startup crash.
- The canonical execution route is exactly one endpoint:
  `POST /v1/guidance/execute`.
  Its request is a versioned envelope with `schema_version` and a normalized
  task object. No second route or legacy alias is added.
- Guidance may deny a task or require approval, but it cannot grant
  permissions, bypass Verdict eligibility, or alter the deterministic routing
  policy.
- The implementation has no package dependency on Ruflo, RuVector, or any
  host CLI. Host adapters and process invocation belong to #108.
- Guidance state is process-local and bounded for this slice. Persistence,
  cross-process coordination, and durable proof storage are follow-ups.

## Configuration contract

| Variable | Default | Meaning |
|---|---:|---|
| `VERDICT_GUIDANCE_ENABLED` | `0` | Explicitly opt into experimental guidance |
| `VERDICT_GUIDANCE_PATH` | unset | Absolute or repository-relative constitution path |
| `VERDICT_GUIDANCE_LOCAL_PATH` | unset | Optional local overlay path |
| `VERDICT_GUIDANCE_INIT_TIMEOUT_MS` | `1000` | Maximum enabled initialization time |
| `VERDICT_GUIDANCE_MAX_BYTES` | `131072` | Maximum size of each guidance document |
| `VERDICT_GUIDANCE_MAX_RULES` | `1000` | Maximum parsed rules across the documents |

The default constitution filename is `GUIDANCE.md`; if it is absent while
guidance is enabled, the service remains available and reports degraded
guidance status. A caller cannot select a source path or enable the feature
through an API request.

## Versioned execution envelope

```json
{
  "schema_version": "1",
  "task": {"goal": "bounded task description", "protected_work": false}
}
```

The response includes `schema_version`, `decision`, `authorization`, the
normalized task, bounded matched-rule metadata, and the policy version.
Unsupported schema versions are rejected with a client error. An enabled but
degraded control plane returns a service-unavailable response with an
actionable status rather than silently executing without guidance.

## Verification plan

- Default lifespan succeeds with no guidance files, package, or local memory
  state present; no guidance initializer is called.
- OpenAPI contains exactly one `/v1/guidance/execute` operation, and the
  request schema is versioned. The operation returns an explicit disabled
  response until the feature is enabled.
- Enabled startup covers healthy, missing, malformed, path-outside-root, and
  timeout cases with bounded degraded responses.
- Concurrent initialization is idempotent and does not create duplicate
  control planes.
- Guidance denial and approval requirements cannot bypass the existing Gate.
- Package, wheel, and clean-install checks do not include local databases,
  backups, host configuration, or optional Ruflo dependencies.

## Rejected alternatives

- **Always load `CLAUDE.md` or `AGENTS.md`:** host-specific and unsafe for
  default startup.
- **Make Ruflo the required dependency:** violates the optional integration
  boundary and makes clean installs depend on a local source checkout.
- **Add host-specific routes:** creates parallel contracts and makes clients
  select an execution host instead of using a stable Verdict boundary.
- **Delete the experimental implementation:** loses the recoverable work
  recorded for #107; it is retained behind the boundary for follow-up work.
