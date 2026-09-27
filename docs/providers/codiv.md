# Codiv Provider Integration

**Status:** Optional OmniRoute execution candidate

## Overview

Codiv models are available through OmniRoute when:
1. The OmniRoute inventory includes routes with the `codiv/` provider prefix
2. `CODIV_API_KEY` environment variable is set

Codiv is an **optional** provider. Missing credentials or inventory do not impact other routes.

## Provider Identity

**Hermetic capability** (proven in CI fixtures):

- Codiv routes are identified by **exact provider prefix matching** (default: `"codiv"`)
- Provider + model form the identity (e.g., `codiv/diffusiongemma-26b-a4b-it`)
- Same model name under a different provider (e.g., `nvidia/diffusiongemma-26b-a4b-it`) is NOT Codiv

Test fixture: Inventory with only `nvidia/diffusiongemma-26b-a4b-it` → 0 Codiv candidates.

## Eligibility Gates

**Hermetic capability** (standard gate application):

Codiv candidates pass through the same gates as all routes:
1. **DISCOVERED**: Present in OmniRoute inventory
2. **ENTITLED**: Has valid `CODIV_API_KEY` credentials
3. **HEALTHY**: Passing health checks
4. **AVAILABLE**: Not rate-limited or quota-exhausted
5. **TASK_ELIGIBLE**: Meets task requirements

No bypass. No special "free route" priority.

Test fixture: Protected workload with Codiv + one paid healthy route → protected work is not forced onto Codiv without positive availability evidence.

## Failure Classification

**Hermetic capability** (proven in CI fixtures):

### 429 Responses

Current implementation maps all 429 to `RATE_LIMITED`. Quota exhaustion uses 402 → `QUOTA`.

- **402 Quota exhausted**: Maps to `QUOTA` failure class, route unavailable until quota resets
- **429 Rate limited**: Maps to `RATE_LIMITED` failure class with bounded retry

Test fixtures:
- 402 response → `QUOTA` state, no retry loop
- 429 response → `RATE_LIMITED` state, bounded cooldown

### 529 Service Overload

- **529**: Maps to `UPSTREAM` failure class (provider infrastructure pressure, not model quality degradation)

Test fixture: 529 response → `UPSTREAM` class (transient, retryable).

### Retry-After Header

**Hermetic capability** (bounds checking):

When present in rate-limit responses, Retry-After is parsed and clamped:
- **Min:** 1 second
- **Max:** 300 seconds
- **Malicious values** (negative, huge, garbage): Clamped to safe bounds

Test fixtures:
- `Retry-After: 60` → 60s cooldown
- `Retry-After: -1` → clamped to 1s
- `Retry-After: 999999` → clamped to 300s
- Missing or garbage → default bounded cooldown

## Receipt Identity

**Hermetic capability** (backward-compatible field additions):

Execution receipts track intended vs executed provider/model:

```json
{
  "intended_provider": "codiv",
  "intended_model": "diffusiongemma-26b-a4b-it",
  "executed_provider": "codiv",
  "executed_model": "diffusiongemma-26b-a4b-it",
  "provider_mismatch": false
}
```

When fallback occurs:
- `intended_provider` ≠ `executed_provider`
- `provider_mismatch` = `true`

Fields are **optional** (backward compatible with receipts written before Codiv failure classification and provider identity fields were added).

## Absence Safety

**Hermetic capability** (tested with fixtures):

Missing `CODIV_API_KEY` or Codiv outage:
- Codiv routes become ineligible with reason logged
- Other provider routes are **unaffected**

Test fixture: Missing `CODIV_API_KEY` → Codiv ineligible, OpenAI/Anthropic/etc still eligible.

## Operational Setup

**Requires operator action** (not hermetic):

### 1. Credentials

```bash
export CODIV_API_KEY="your-codiv-api-key"
```

### 2. OmniRoute Configuration

Configure OmniRoute to include Codiv provider with `codiv/` prefix:

```yaml
# Example OmniRoute config (adjust to actual format)
providers:
  - name: codiv
    prefix: "codiv"
    endpoint: "https://api.codiv.example/v1"
    models:
      - diffusiongemma-26b-a4b-it
```

### 3. Smoke Test

Run the smoke test to verify direct and OmniRoute-mediated calls:

```bash
python scripts/smoke_codiv.py
```

**Expected:**
- Exit 2 with "CODIV_API_KEY missing" if key not set
- Exit 0 with response samples if key is valid

This script is **not run in CI** (requires live credentials and network).

## Performance Characteristics

**Live data required** (not hermetic):

Latency and cost data require live `CODIV_API_KEY` and OmniRoute configuration.

Sample collection (to be run manually):
- Direct Codiv API latency: TBD
- OmniRoute-mediated latency: TBD
- Cost per 1K tokens: TBD

## Configuration

```python
# Environment variables
CODIV_PROVIDER_PREFIX = os.getenv("CODIV_PROVIDER_PREFIX", "codiv")
CODIV_API_KEY = os.getenv("CODIV_API_KEY")  # Optional, absence is safe
```

## References

- **OpenSpec change:** `openspec/changes/bod-198-codiv-candidate/`
- **Failure classification:** `verdict/gateway_adapters.py` (`NormalizedFailureClass`)
- **Eligibility gates:** `verdict/eligibility.py`
- **Receipt schema:** `verdict/orchestration/receipt.py`
