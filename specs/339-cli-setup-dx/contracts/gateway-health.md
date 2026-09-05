# Gateway Health Endpoint Contract

**Feature**: 339-cli-setup-dx | **Date**: 2026-09-05
**Verified against**: OmniRoute at localhost:20128 on 2026-09-05

---

## Purpose

This contract defines what Verdict's gateway detection logic MUST verify before accepting a discovered port as a valid gateway. TCP connect alone is not sufficient (FR-006).

---

## Detection Protocol

### Step 1: TCP Connect

- Target: `127.0.0.1:{port}` for ports in `[20128, 20129, 20132]`
- Timeout: 500ms
- On failure: mark port unavailable; continue to next port

### Step 2: Health Check

```
GET /api/health
Host: localhost:{port}
```

**Expected response**:
- HTTP status: `200`
- Content-Type: `application/json`
- Body must contain: `{"status": "ok"}` (additional fields permitted)

If status is not `"ok"` or HTTP status is not `200`, this port is NOT a valid gateway.

### Step 3: Identity Resolution (Optional, for labeling)

```
GET /api/settings
Host: localhost:{port}
```

**Key field**:
```json
{
  "runtimePorts": {
    "port": 20128
  }
}
```

Use `runtimePorts.port` to confirm the gateway's self-reported port. Cross-reference:

| Self-reported port | Label |
|---|---|
| 20128 | OmniRoute |
| 20129 | 9router |
| 20132 | OmniRoute (alternate) |
| Other | Unknown |

If `/api/settings` is unreachable, fall back to port-to-label mapping above. Do not fail detection on Step 3 failure.

---

## Contract Stability

- `/api/health` → `{"status":"ok"}` is treated as a stable contract.
- `/api/settings` → `runtimePorts` is a best-effort identity hint; detection must not hard-depend on it.
- If OmniRoute's health endpoint changes, the detection code in `provider_detection.py` must be updated and this contract updated with the new verified response.

---

## Non-Gateway Cases

A port that responds to TCP but fails the health check is classified as:
- **Occupied (non-gateway)**: report to user as "port {N} in use by another service" — do not claim it as a gateway.
