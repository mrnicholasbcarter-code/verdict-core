# Background agentic qualification

`prove-at-rest` is the existing consented, budgeted health-cache prober.
It writes `health-cache.json`, not the legacy cycle document.

## Import controller session evidence

```bash
verdict prove-at-rest import-sessions worker-outcomes.jsonl --json
```

Only use a trusted controller ledger. Worker self-reports are not proof.
The BOD-299 importer accepts `session_canary_v1` and `free_real_task` outcomes.
Passes qualify the exact route. Failures revoke qualification.
The cache retains the evidence timestamp, source path, and child id.
Imports are idempotent. Older evidence cannot replace newer evidence.
Imports do not refresh liveness, identity, capacity, or entitlement.
Session capability lasts 7 days by default. Set
`VERDICT_AGENTIC_CAPABILITY_TTL_S` to a positive finite number of seconds to
change it. Invalid settings fail closed. Newer exact-route session failures
revoke capability immediately. Liveness keeps the existing short health gate
and cheap bounded probe. Selection requires capability and current liveness.
Without imported session evidence, the existing 600-second agentic probe TTL
still applies. Session-score narrowing also remains active.

## Install the user service

First inspect the plan. This does not write files or run `systemctl`:

```bash
verdict prove-at-rest install-service --interval 300 --max-requests 300 --dry-run --json
```

To explicitly consent to background live probes:

```bash
verdict prove-at-rest install-service --interval 300 --max-requests 300 --json
```

This writes `verdict-prove-at-rest.service` and `verdict-prove-at-rest.timer`
in `$XDG_CONFIG_HOME/systemd/user` (default `~/.config/systemd/user`).
The timer starts the existing daemon. The daemon bounds each cycle to the
request budget and 600 wall seconds. It controls the cycle interval.
The unit uses the installing interpreter and repository directory.
Configure the OmniRoute endpoint in the user service environment before use.
Do not put credentials in the unit files. Verdict also loads its credential store.
Repeat installation safely. Changed units reload and restart an active daemon.

```bash
verdict prove-at-rest uninstall-service --json
```

Uninstall stops the daemon and timer before removing their files.
It does not delete the health cache or session ledger.

## Eligibility

The legacy `verdict eligibility` view reports state counts across the full
health cache, separate from evaluated routes or a bounded refresh sample.
It also reports current agentic capability counts by the selector's capacity
class. Unknown capacity stays unknown. These counts are diagnostic; they do
not bypass admission or grant launch authority.
