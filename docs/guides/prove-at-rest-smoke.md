# Smoke: prove-at-rest daemon (free∩active only)

Background prover for **free-tier ∩ active-provider** OmniRoute identities.
Paid/frontier and inactive/unconnected models are never probed; admit drops are
recorded as `skipped` with named reasons. Request-time budgeted confirm probes
are out of scope.

State is written to `~/.verdict/prove-at-rest/state.json` (override with
`--state-path` or `VERDICT_PROVE_AT_REST_STATE`). Healthy rows carry a
`ModelPassport` payload Core can load via `verdict.prove_at_rest.load_healthy_passports`
for later admit.

## Prerequisites

Same OmniRoute tunnel + env as [free-tier-admit-smoke.md](free-tier-admit-smoke.md):

```bash
export OMNIROUTE_BASE_URL=http://127.0.0.1:20128
export OMNIROUTE_API_KEY=…   # never print
```

## One-shot prove

```bash
uv run python -m verdict prove-at-rest once --allow-live-probe --json
```

Expect:

- `admitted` lists only free∩active concrete identities
- `results[].status` is `healthy`, `failed`, or `skipped`
- `skipped` rows include named `reason` (`inactive_unconnected`, `opaque_auto`,
  `metadata_ghost`, `not_free_tier`, …)
- Paid catalog rows (e.g. Opus) do **not** appear as probed identities
- State file path is reported as `state_path`

## Status

```bash
uv run python -m verdict prove-at-rest status --json
```

## Daemon

```bash
uv run python -m verdict prove-at-rest daemon --allow-live-probe --interval 300
```

Cycles every `--interval` seconds until interrupted (Ctrl-C).

## Fixture tests (no live OmniRoute)

```bash
uv run pytest tests/test_prove_at_rest.py -q
```
