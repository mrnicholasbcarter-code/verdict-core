# User journey: install → provider → route → orchestrate → mission → failover → replay

This is the shortest truthful path through Verdict Core. The commands below are
local and bounded unless explicitly marked as an optional live-provider
operation; failover proof runs get unique session IDs. A catalog entry is not
evidence that a provider is reachable.

## Install

```bash
uv sync --extra dev
verdict --help
```

Evidence: the package installs and the CLI exposes the journey commands. This
does not configure a provider or make a network call.

## Provider and route

Use `verdict detect --offline --json` for a no-probe offline provider status
report. The command returns before live discovery, so it does not open sockets,
invoke provider CLIs, read credentials, or send a prompt. A catalog record is
not proof of reachability; protected work requires a fresh capability passport.

For a credential-free route decision, run the checked-in fixture:

```bash
verdict quickstart --non-interactive --dry-run --json
```

For an operator-configured live route, the syntax is positional. It executes a
completion through the configured provider and fails closed when no qualified
route remains:

```bash
verdict route "summarize this change" --criticality low --terse
```

Use `verdict simulate "summarize this change"` for a no-send forecast.
`--allow-offline` disables live discovery and probes; it exits non-zero with
`transport_outcome=error` because no provider completion was sent. The separate
`--allow-legacy-selector` flag is only the explicit pre-the execution-path authority optimizer (ADR-035) migration escape.

## Orchestrate: goal to receipt

`verdict orchestrate` runs the full goal-to-receipt pipeline (ADR-036): frontier
planner decomposes the goal into a DAG, each node gets an eligible route, workers
run in parallel, faults trigger same-node reroute, an independent reviewer checks
output, and a digest-verified receipt is written.

```bash
# Full run (requires OmniRoute gateway on localhost:20128)
verdict orchestrate "Add a tested textkit.stats feature"

# With chaos flags (quota + no-final-answer injected for specific routes)
verdict orchestrate "Add a tested textkit.stats feature" \
  --inject "cc/claude-sonnet-4-6=quota,no_final" \
  --inject "cc/*=rate_limit"

# Live TUI view while a run is in progress
verdict watch <run-id>

# Inspect the eligibility ladder
verdict eligibility

# Verify the receipt (event-log digest) after completion
verdict run-receipt <run-id>
```

See [`docs/guides/orchestration-golden-path.md`](guides/orchestration-golden-path.md) for
prerequisites, scenario matrix (A–J), and recorded evidence.

## Mission, failover, and replay

The offline proof path is credential-free:

```bash
verdict autodev-golden-path --objective "verify a small repository" --repo /tmp/repo --json
verdict failover-proof --memory-path /tmp/failover.db --json
VERDICT_MEMORY_DB=/tmp/failover.db verdict replay <session-id> --json
```

The golden path is an offline proof over a clean Git repository: discovery,
durable memory, and bounded verification. It does not generate code or call an
LLM. The replay command loads a previously persisted execution session; it is a
state reload, not an event-log verifier or a new live run.

Failover is proven by the dedicated offline proof in
`verdict.failover_replay_proof`: a forced HTTP 429 is recorded, an eligible
replacement is selected, completed stages are not repeated, and the event
sequence is included in the proof payload. `verdict replay` reloads the
persisted execution session and shows the completed steps. This is simulated
provider failure, not evidence of a live provider's reliability.

Each path emits bounded, privacy-safe evidence.

## Maturity matrix

| Capability | Status | Evidence | Limitation |
|---|---|---|---|
| Contracts and eligibility gates | production functional | contract, security, and eligibility tests | provider behavior remains external |
| Credential-free quickstart | production functional | `quickstart` CLI and fixture tests | demo candidates are not real providers |
| Autonomous-dev golden path | production functional | `autodev-golden-path` tests | no claim of LLM generation |
| Goal-to-receipt orchestration | production functional (live, faults injected) | GOLDEN_PATH_CERTIFICATION.md, scenarios A–J, 2907 tests | requires OmniRoute gateway; reviewer requires `ocr` on PATH |
| Forced failover and replay | production functional | `failover-proof` CLI | simulated provider failure |
| Live provider routing | production path, externally contingent | consent-gated probes, execution receipts, and fail-closed identity checks | authorization, quota, health, and live model output remain external |
| Adaptive/quality/cost claims | simulated only | benchmark fixtures and reports | not a production quality guarantee |
| Dashboard and ecosystem adapters | functional but incomplete | package/import and focused adapter tests | deployment and cross-repo operation are not proven here |
| External provider health/quota | missing | not tested in CI | requires live credentials and network |
