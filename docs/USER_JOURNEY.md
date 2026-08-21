# User journey: install → provider → route → mission → failover → replay

## Install

```bash
uv sync --extra dev
verdict --help
```

## Provider and route

Use `verdict detect` to inspect locally configured providers. A catalog record is
not proof of reachability; protected work requires a fresh capability passport.
Use `verdict route --task "..." --json` only when explicit routing is enabled.

## Mission, failover, and replay

The offline proof path is credential-free:

```bash
verdict autodev-golden-path --objective "verify a small repository" --repo /tmp/repo --json
verdict replay <session-id> --json
```

Each path emits bounded, privacy-safe evidence. Failover resumes after the last
committed stage; replay is verification of recorded events, not a new live run.

## Maturity matrix

| Capability | Status | Evidence | Limitation |
|---|---|---|---|
| Contracts and eligibility gates | production functional | CI contract/security jobs | provider behavior remains external |
| Autonomous-dev golden path | production functional, offline | `autodev-golden-path` tests | no claim of LLM generation |
| Forced failover and replay | production functional, offline | issue #267 proof | simulated provider failure |
| Live provider routing | functional but incomplete | explicit opt-in integration tests | task routing remains disabled by operator policy |
| Adaptive/quality claims | simulated or advisory | benchmark fixtures | not a production quality guarantee |
