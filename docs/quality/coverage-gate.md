# Critical Module Coverage Gate

## Purpose

Prevents regression in safety-critical modules by enforcing branch coverage floors.

## Critical Modules

The following modules have coverage floors based on their measured branch coverage at the the eligibility-gate default construction fix baseline:

- **verdict/eligibility.py** (85%): Candidate eligibility and admission logic
- **verdict/contracts.py** (80%): Structured task/routing contracts
- **verdict/availability_cache.py** (65%): Provider availability caching
- **verdict/subagent_resolver.py** (50%): Subagent selection and resolution
- **verdict/security.py** (75%): Security and validation boundaries
- **verdict/orchestration/*** (65-90%): Orchestration authority paths

Each floor is the measured coverage rounded DOWN to the nearest 5%, ensuring the gate never raises the bar above current reality.

## Usage

```bash
# Run tests with branch coverage
pytest --cov=verdict --cov-branch --cov-report=xml

# Check critical module floors
scripts/check_critical_coverage.py

# Or with custom paths
scripts/check_critical_coverage.py --coverage-xml path/to/coverage.xml --config path/to/config.toml
```

### Exit Codes

- **0**: All modules meet or exceed their floors
- **1**: One or more modules below floor, OR configured module not measured in coverage.xml
- **2**: Missing coverage.xml or config file, or parse error (usage error)

A configured module that is absent from coverage.xml is treated as a test failure (exit 1)
with output `✗ module/path.py NOT MEASURED (floor: X%)`, not a silent 0.0%.

## Configuration

Floors are stored in `.coverage-critical.toml` with the measured baseline for reference:

```toml
[critical_modules."verdict/eligibility.py"]
branch_coverage_floor = 85
measured_at_baseline = 88.24
```

## Adding to CI

Add this step after the test suite:

```yaml
- name: Check critical module coverage
  run: |
    pytest --cov=verdict --cov-branch --cov-report=xml
    scripts/check_critical_coverage.py
```

## Rationale

This gate protects critical authority modules by criticality rather than chasing raw line count. A module with 50% coverage that handles subagent selection gets a 50% floor; a module with 90% coverage that handles orchestration receipts gets a 90% floor. The gate prevents regression without imposing unrealistic targets.
