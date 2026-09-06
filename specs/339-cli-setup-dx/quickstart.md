# Quickstart Validation Guide: CLI Setup DX

**Feature**: 339-cli-setup-dx | **Date**: 2026-09-05

This guide documents how to validate that the feature works end-to-end. It is for reviewers and testers, not an installation document.

---

## Prerequisites

- Python 3.10+ installed
- `pip` or `pipx` available
- `uv` installed (for contributor scenario)
- OmniRoute running at localhost:20128 (for gateway autodetect scenarios)

---

## Scenario 1: End-User Setup with Local Gateway (SC-001, SC-002, SC-003)

**What it proves**: FR-001 through FR-005, FR-008 — zero-friction setup with autodetected gateway.

```bash
# 1. Confirm OmniRoute health (should return {"status":"ok"})
curl -s http://localhost:20128/api/health

# 2. Remove any existing config to start clean
rm -f ~/.config/verdict/verdict.yaml

# 3. Run the copy-paste setup command (setup script must be idempotent)
bash <(curl -fsSL https://raw.githubusercontent.com/mrnicholasbcarter-code/verdict-core/main/install.sh)
# OR from a local checkout:
bash install.sh

# 4. Verify gateway was autodetected and written to config
grep gateway_url ~/.config/verdict/verdict.yaml
# Expected output: gateway_url: http://localhost:20128

# 5. Verify config passes check
verdict check
# Expected: exits 0, no errors

# 6. Run quickstart
verdict quickstart
# Expected: exits 0 with a routing result, no "OMNIROUTE_BASE_URL not set" error

# 7. Timing check (wall clock)
time bash install.sh
# Expected: real < 5m0.000s
```

**Pass criteria**: All commands exit 0; `verdict.yaml` contains a reachable `gateway_url`; wall clock under 5 minutes.

---

## Scenario 2: Gateway Detection Accuracy (SC-002)

**What it proves**: FR-006, FR-007, FR-014 — health check gating, identity labeling, corrected port assignments.

```bash
# 1. With OmniRoute at 20128 running:
verdict detect --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d, indent=2))"
# Expected: entry with port=20128, health_ok=true, identity="omniroute"

# 2. Without OmniRoute running (kill it first):
# pkill -f omniroute  # or stop via your process manager
verdict detect --json
# Expected: no entries with health_ok=true; message "no local gateway found"

# 3. With a non-Verdict service on 20128 (e.g., python3 -m http.server 20128):
verdict detect --json
# Expected: port 20128 appears as "occupied (non-gateway)", NOT as a valid gateway
```

**Pass criteria**: `health_ok` is only `true` when `/api/health` returns `{"status":"ok"}`; `identity` matches the port's actual service.

---

## Scenario 3: Environment Variable Reference (SC-004)

**What it proves**: FR-009 — `.env.example` is complete.

```bash
# Count env vars read in source
grep -rn 'os\.environ\[.*\]\|os\.getenv(' verdict/ --include="*.py" \
  | grep -v __pycache__ | grep -v 'test_' \
  | grep -oP '(?<=getenv\(|environ\[)["\x27][A-Z_]+["\x27]' \
  | sort -u > /tmp/source-vars.txt

# Count env vars in .env.example
grep -E '^[A-Z_]+=\|^# [A-Z_]+=' .env.example \
  | grep -oP '^[A-Z_]+' | sort -u > /tmp/example-vars.txt

# Diff: all source vars must appear in .env.example
diff /tmp/source-vars.txt /tmp/example-vars.txt
# Expected: no lines unique to source-vars.txt (all vars documented)
```

**Pass criteria**: Zero environment variables read in source that are absent from `.env.example`.

---

## Scenario 4: Bug Fixes Verified (FR-012, FR-013, FR-014)

**What it proves**: The four blocking bugs are fixed.

```bash
# FR-012: quickstart.sh writes verdict.yaml not config.yaml
rm -f ~/.config/verdict/verdict.yaml ~/.config/verdict/config.yaml
bash quickstart.sh --dry-run 2>&1 | grep 'config'
# Expected: "verdict.yaml" appears, "config.yaml" does NOT appear as write target

# FR-013: cost-report is registered as a subcommand
verdict --help | grep 'cost-report'
# Expected: "cost-report" appears in subcommand list

# FR-014: port labels are correct
python3 -c "from verdict.provider_detection import GATEWAY_PORTS; print(GATEWAY_PORTS)"
# Expected: OmniRoute maps to 20128, 9router to 20129

# FR-011: doctor detects wrong config filename
cp ~/.config/verdict/verdict.yaml ~/.config/verdict/config.yaml 2>/dev/null || true
rm -f ~/.config/verdict/verdict.yaml
verdict doctor
# Expected: output contains "config.yaml detected; should be verdict.yaml" and offers rename
```

**Pass criteria**: All four checks produce the expected output.

---

## Scenario 5: Contributor Dev Setup (FR-015)

**What it proves**: `scripts/dev-start.sh` sets up a working dev environment.

```bash
# From a fresh clone
cd /tmp && git clone https://github.com/mrnicholasbcarter-code/verdict-core.git verdict-fresh
cd verdict-fresh

# Run dev setup
bash scripts/dev-start.sh

# Verify: server starts (check output), tests available
uv run pytest tests/ -x -q --timeout=60
# Expected: passes with 0 failures (or known-skip count matching CI baseline)
```

**Pass criteria**: `scripts/dev-start.sh` completes without errors; `uv run pytest` passes.

---

## Scenario 6: Regression Check

```bash
# Run full test suite from repo root after all changes
cd /home/nick/dev/verdict-core
uv run pytest tests/ -x -q 2>&1 | tail -5
# Expected: all tests pass; 0 new failures
```

---

## References

- Config schema: [contracts/config-schema.yaml](contracts/config-schema.yaml)
- Gateway health contract: [contracts/gateway-health.md](contracts/gateway-health.md)
- Env var taxonomy: [data-model.md](data-model.md)
- Research findings: [research.md](research.md)
