# User journey: install → provider → route → mission → failover → replay

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

For an operator-configured route preview, the syntax is positional. This is a
local policy decision and does not send the prompt to a model:

```bash
verdict route "summarize this change" --criticality low --terse
```

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
| Forced failover and replay | production functional | `failover-proof` CLI | simulated provider failure |
| Live provider routing | functional but incomplete | consent-gated transport/probe tests | authorization, quota, health, and live model output are external |
| Adaptive/quality/cost claims | simulated only | benchmark fixtures and reports | not a production quality guarantee |
| Dashboard and ecosystem adapters | functional but incomplete | package/import and focused adapter tests | deployment and cross-repo operation are not proven here |
| External provider health/quota | missing | not tested in CI | requires live credentials and network |
