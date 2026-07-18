# llm-gate Enforcement and Learning Integration
## Mandatory intelligence contract with a deterministic safety floor

### 1. Design rule

The public `llm-gate` package MUST expose a stable `IntelligenceService` contract independent of Claude Code, Codex, Cursor, Jcode, Hermes, Cowork, or any agent framework. The production profile MUST use Ruflo/RuVector as its managed adaptive intelligence backend. A deterministic local backend is always present as the safety floor and cold-start path, but running without the managed backend is an explicitly named development-only degraded mode, not a production configuration.

The integration MUST remain protocol-based. It MUST NOT couple the public package to private Ruflo, RuVector, OmniRoute, or agent-tool database tables.

### 2. Core enforcement layer

The core owns:

- typed configuration and policy versions;
- request parsing and protocol validation;
- protected-task classification;
- model capability and availability gates;
- prompt redaction and privacy policy;
- retry/fallback legality;
- decision explanations;
- outcome event schema;
- local JSONL or SQLite persistence;
- deterministic regression tests.

A policy decision is allowed to fail closed for protected operations and fail safe for non-protected routing. The failure mode must be visible in the decision event.

### 3. Ruflo adapter

Ruflo integration is a subprocess or plugin adapter with strict timeouts and structured output. It may call only documented surfaces:

- `ruflo guidance gates --command ... --content ... --json` for command/content enforcement;
- `ruflo hooks model-route -t ... --prefer-quality --context ...` for advisory model-family routing;
- `ruflo hooks model-outcome -t ... -m ... -o ... -q ...` for coarse model-family feedback;
- `ruflo neural router decide ...` and `train-from-trajectories` for offline/inspection workflows;
- `ruflo hooks pretrain` and `guidance compile` only during an explicit setup or build step, never implicitly during a request.

The Ruflo `model-route`/`model-outcome` CLI currently accepts Claude-family labels such as `haiku`, `sonnet`, and `opus`, not arbitrary OmniRoute IDs. The adapter MUST map arbitrary model IDs to a stable family label and preserve the original ID in llm-gate telemetry. If a safe mapping is impossible, skip the Ruflo call and continue with the local policy.

### 4. RuVector intelligence adapter

RuVector SONA is the required managed adaptive signal for the production profile. It is an asynchronous learning sink and advisory scorer:

- `ruvector hooks trajectory-begin --context ... --agent ... --file ...`;
- `ruvector hooks trajectory-step --action ... --result ... --reward ...`;
- `ruvector hooks trajectory-end --success --quality ...`;
- `ruvector sona train <json>` only in an offline/batch worker;
- `ruvector sona patterns <query> --json` for bounded advisory retrieval.

The adapter MUST NOT read `ruvector.db`, Ruflo databases, OmniRoute SQLite storage, or any undocumented table directly. It exchanges versioned JSON events. If the adapter is unavailable, production readiness MUST fail closed at the service boundary, while the deterministic backend remains available for explicit degraded development mode.

### 5. Event schema

```json
{
  "event_version": "1",
  "request_id": "uuid",
  "task_fingerprint": "sha256:...",
  "task_class": "implementation",
  "model_id": "aug/gpt-5.4-medium",
  "model_family": "gpt",
  "provider": "aug",
  "policy_version": "policy-2026-07-13.1",
  "decision": "selected|fallback|escalated|denied",
  "transport_outcome": "success|timeout|rate_limited|upstream_error|parse_error",
  "quality_outcome": "unknown|success|failure|user_rejected|tests_failed",
  "quality_score": null,
  "latency_ms": 0,
  "input_tokens": null,
  "output_tokens": null,
  "safety_flags": [],
  "timestamp": "2026-07-13T00:00:00Z"
}
```

For training and offline inspection, the core also emits a versioned privacy-safe `task_workflow_outcome_episode` record. The episode stores only redacted summaries and stable fingerprints for task intent, workflow shape, and execution outcome. It MUST NOT persist raw prompts, credential material, or full context blobs by default. Instead it records fields such as task fingerprint and redacted preview, task class/privacy/risk, context key names, workflow step counts and verification checks, selected route metadata, retry/fallback counts, and redacted outcome details.

### 6. Learning safety

- No raw prompt or completion is sent to learning sinks by default.
- Redaction occurs before any external process invocation.
- Learning calls are asynchronous and bounded by a small latency budget.
- A failed learning process never corrupts or crashes an in-flight user request. It does flip managed-intelligence readiness to not-ready and records the failure. The active profile then either rejects protected work or explicitly continues in visible degraded development mode.
- A learned model cannot override a hard gate, denied model, quality floor, or privacy policy.
- New policies start with a cold-start deterministic mode.
- Training data is append-only, versioned, and exportable for audit.
- User opt-out disables new prompt-derived collection and training for the opted-out scope. Deterministic policy intelligence continues to run, and any adaptive snapshot used must already be approved for that privacy scope. A global opt-out that makes the required managed backend unavailable leaves the production profile not-ready rather than silently pretending that intelligence is still active.

### 7. Ruflo guidance integration

Project bylaws should be compiled into a policy bundle during release/setup, not guessed from a prompt. The bundle should define:

- required research-before-edit behavior for delegated agents;
- allowed tools and destructive-command gates;
- required test/build/package checks;
- no-secrets and no-raw-prompt rules;
- evidence required before a work item can move to Done.

`llm-gate` may invoke a guidance gate for a command or content check, but it remains responsible for its own request-routing policy and must treat guidance failures as explicit policy outcomes.

### 8. Observability

Expose counters for policy denials, escalations, unknown catalog rows, selected model families, fallback rates, learning adapter errors, quality feedback, and saved frontier calls. Do not expose raw prompts or credentials.

### 9. Future suggestion service

Suggestion generation is intentionally separate from mandatory request intelligence. It may consume validated, redacted outcome aggregates and RuVector/Ruflo patterns to produce ranked opportunities with evidence, confidence, expected impact, novelty, expiry, and a proposed next experiment. It is non-blocking and advisory. No suggestion may mutate routing policy, invoke tools, or alter a repository without explicit approval and a new tracked work item.
