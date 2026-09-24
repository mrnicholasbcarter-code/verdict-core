# Golden path

> **Scope:** This is the basic live-probe path. For the end-to-end goal,
> orchestration, worker, review, and receipt path, see the
> [interview golden path](interview-golden-path.md).

Run this after `pip install verdict-core`. If a live step cannot talk to the
gateway, the result is **blocked**, not a pass.

## 1. Offline quickstart

```bash
verdict quickstart --non-interactive --dry-run
```

Expected: a deterministic, read-only quickstart result. It does not require an
API key and does not prove live gateway health.

## 2. See the local gateway

```bash
verdict detect --json
```

Inspect the detected gateway entries for OmniRoute at
`http://localhost:20128/v1` and `server_running: true`. If it is false, stop.
Do not use fixture data as live proof.

## 3. Live probe (consent required)

Probe a **named** model, not `auto/*`:

```bash
verdict probe <named-model-id> \
  --base-url http://localhost:20128/v1 \
  --allow-live-probe \
  --json
```

A successful result has `"ok": true` and `"status": "ready"`. A timeout or
`degraded` result is blocked, not live-readiness proof.

## 4. Optional catalog qualification

```bash
verdict catalog --base-url http://127.0.0.1:20128 --management --json
```

This command qualifies the documented management catalog projection. A timeout
or malformed catalog is not a pass. `verdict models` shows Verdict's qualified
catalog used for routing and simulation; it is not, by itself, proof that the
local gateway is live.

## What this proves

This path proves only the steps you actually run: an offline quickstart, local
gateway detection, and, with explicit consent, one named live liveness probe.
Use [interview golden path](interview-golden-path.md) when you need the
orchestration receipt path.
