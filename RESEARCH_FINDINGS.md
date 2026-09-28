# Research Findings: diagnose_shared_memory Implementation

## Summary
Located and analyzed the `diagnose_shared_memory` function in `verdict/runtime_certification.py` 
to understand how provider status values map to diagnostic reason strings.

## Key Finding
**The function currently maps both `auth_failed` and `degraded` provider status to the same 
`unwritable` reason, but the requirement is to report them distinctly.**

## Current Implementation

### Location
- **File**: `verdict/runtime_certification.py`
- **Function**: `diagnose_shared_memory`
- **Lines**: 690-738

### Status-to-Reason Mapping

The function processes provider health reports and returns one of these reason prefixes:

1. **`unreachable:`** - When status is `unavailable`, `timeout`, or health_fn raises an exception
2. **`schema_incompatible:`** - When status is `incompatible`, `malformed`, or state is `incompatible`
3. **`unwritable:`** - When status is `degraded` **OR** `auth_failed` ⚠️
4. **`ok`** - For all other cases (healthy/available)

### The Problem (Lines 741-745)

```python
# Unwritable — provider reports degraded or auth failure
if status_raw in {"degraded", "auth_failed"}:
    return SharedMemoryDiagnosis(
        state=CertificationState.DEGRADED,
        reason=f"unwritable: {report.get('message', status_raw)}",
    )
```

**Current behavior:**
- `status='auth_failed'` → `reason='unwritable: ...'` ❌ **INCORRECT**
- `status='degraded'` → `reason='unwritable: ...'` ✅ **CORRECT**

## Required Change

Split the combined check into two separate conditions:

```python
# Auth failure
if status_raw == "auth_failed":
    return SharedMemoryDiagnosis(
        state=CertificationState.DEGRADED,
        reason=f"auth_failed: {report.get('message', status_raw)}",
    )

# Unwritable — provider reports degraded
if status_raw == "degraded":
    return SharedMemoryDiagnosis(
        state=CertificationState.DEGRADED,
        reason=f"unwritable: {report.get('message', status_raw)}",
    )
```

This preserves:
- The existing message format (prefix + colon + message)
- The behavior for `degraded` status → `unwritable` reason
- All other status mappings (unreachable, schema_incompatible, ok)

## Test Impact

### Unit Tests (`tests/test_runtime_certification.py`)

**`test_unwritable()` (line 461):**
- Uses `status='auth_failed'` with message `"token expired"`
- Currently expects `"unwritable"` in reason
- **Will need update** to expect `"auth_failed"` instead

**`test_unwritable_degraded_status()` (line 471):**
- Uses `status='degraded'` with message `"disk full"`
- Expects `"unwritable"` in reason
- **Should continue to pass** unchanged

### Integration Tests (`tests/test_doctor_shared_memory.py`)

**`test_doctor_shared_memory_unwritable()` (line 146):**
- Runs the real `cmd_doctor --json` entry point
- Mocks provider with `status='auth_failed'`, message `"authentication failed"`
- Currently expects `"unwritable"` in `diagnosis_reason`
- **Will need update** to expect `"auth_failed"` instead
- This test validates the full integration path through `cmd_doctor`

## Integration with cmd_doctor

The `diagnose_shared_memory` function is called from `_collect_doctor_diagnostics` 
in `verdict/cli.py` (around line 2480):

```python
if isinstance(shared_memory, dict) and shared_memory.get("configured"):
    from verdict.runtime_certification import diagnose_shared_memory
    
    sm_diagnosis = diagnose_shared_memory(health_fn=lambda: shared_memory)
    shared_memory["diagnosis_state"] = sm_diagnosis.state.value
    shared_memory["diagnosis_reason"] = sm_diagnosis.reason
    if sm_diagnosis.state.value == "degraded":
        warnings_found.append(f"shared memory degraded: {sm_diagnosis.reason}")
```

The reason string is:
1. Stored in the JSON output under `shared_memory.diagnosis_reason`
2. Included in warnings shown to users
3. Displayed in both `--json` and text output modes

## Acceptance Criteria Coverage

✅ **"diagnose_shared_memory returns a reason starting with 'auth_failed' when provider status is auth_failed"**
- Requires changing line 745 to use prefix `auth_failed` instead of `unwritable`

✅ **"'unwritable' is still reported for real write-permission failures"**
- Preserves `status='degraded'` → `unwritable` mapping

✅ **"integration test runs the real doctor entry point in a tmp HOME and shows the new reason"**
- `test_doctor_shared_memory_unwritable` already exists and tests this full path
- Just needs assertion update to check for `auth_failed` instead of `unwritable`

## Implementation Strategy

1. **Split the combined condition** in `runtime_certification.py` (lines 741-745)
2. **Update unit test** `test_unwritable` in `test_runtime_certification.py` (line 469)
3. **Update integration test** `test_doctor_shared_memory_unwritable` (line 172)
4. **Verify** all tests pass with `.venv/bin/python -m pytest -q tests/test_runtime_certification.py tests/test_doctor_shared_memory.py`

## Files Referenced

- `verdict/runtime_certification.py` - Main implementation
- `tests/test_runtime_certification.py` - Unit tests
- `tests/test_doctor_shared_memory.py` - Integration tests
- `verdict/cli.py` - Doctor command that calls diagnose_shared_memory
