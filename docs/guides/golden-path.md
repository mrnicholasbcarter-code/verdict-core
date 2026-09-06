# Golden path

Run this after `pip install verdict-core`. If a live step cannot talk to the gateway, the result is **blocked**, not a pass.

## 1. Offline quickstart

```bash
verdict quickstart --non-interactive --dry-run
```

Expected: selected route `demo/frontier-tools`. Named exclusions for missing tools, exhausted quota, and unknown health. No files written. No API key.

## 2. See the local gateway

```bash
verdict detect --json
```

Look under `centralized_routers` / `gateways` for OmniRoute at `http://localhost:20128/v1` with `server_running: true`. If it is false, stop. Do not use fixture data as a live proof.

## 3. Live probe (consent required)

Probe a **named** model, not `auto/*`:

```bash
verdict probe task-coding \
  --base-url http://localhost:20128/v1 \
  --allow-live-probe \
  --json
```

Expected when the gateway is healthy: `"ok": true`, `"status": "ready"`. Timeout or `degraded` means blocked.

## 4. What is not this path

- `verdict catalog --base-url http://127.0.0.1:20128 --management --json` should return `"passed": true` on a live OmniRoute catalog. A fetch timeout is `catalog_fetch_timeout`, not a pass. Dual public+management reconcile can still fail closed if the two projections disagree.
- `verdict models` without a live catalog still shows the local config identity (often a single Anthropic floor). That is not a live catalog proof.
- Cookie or browser-quota probes are a later feature. They are not required here.

## Recorded run (2026-09-06)

| Step | Result |
|------|--------|
| `pip install .` from `origin/main` `3cc8231` | pass |
| `verdict quickstart --non-interactive --dry-run` | pass, `demo/frontier-tools` |
| `GET http://localhost:20128/v1/models` | HTTP 200, 3167 ids |
| `verdict detect` OmniRoute `server_running` | true |
| `verdict probe task-coding --allow-live-probe` | ready |
| `verdict probe auto/best-coding --allow-live-probe` | timeout / degraded (not a live proof) |
| `verdict catalog --management` | 2026-09-06 later: `passed: true`, 3167 rows (fetch timeout was 10s vs ~8s payload) |
