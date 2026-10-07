# Release Certification

This directory contains documentation and tooling for verdict-core's immutable, SHA-bound release certification bundles.

## Overview

Release certification is an executable, repeatable product feature. Instead of hand-edited certification documents, we generate machine-readable evidence bundles bound to an exact git SHA.

## Quick Start

### Running Certification

From the verdict-core repository root:

```bash
# Ensure dependencies are synced
uv sync --frozen --extra dev --extra server

# Run certification (without rehearsals)
python scripts/certify_release.py

# Run with rehearsals
python scripts/certify_release.py \
  --rehearsal clean=/path/to/clean-run \
  --rehearsal chaos=/path/to/chaos-run

# Allow dirty tree (forces INCOMPLETE verdict)
python scripts/certify_release.py --allow-dirty
```

### Output Location

Certification bundles are written to:

```
artifacts/certification/<sha>/
```

Where `<sha>` is the current git HEAD SHA. The default SHA directory is checked and removed before early preflight failures (including a dirty checkout or missing venv), so an old bundle cannot masquerade as the latest result. A complete bundle is built in a same-filesystem staging directory, then renamed into place. The runner checks HEAD before and after each step and again just before publish; if HEAD changes (even to another clean commit), it aborts instead of publishing evidence under the original SHA. Failed staging is removed; the destination is never a partially written bundle. Custom `--output-dir` locations must be empty and their existing files are never deleted. The default directory must be ignored and contain no tracked files before deletion. An independent producer verifier is still missing; this change does not produce a `CERTIFIED` verdict.

## Bundle Structure

A certification bundle contains the following files. Step-specific reports are projections of the manifest step evidence; they never substitute for a missing tool report.

A complete certification bundle contains:

### `manifest.json`

Top-level manifest with:
- `schema_version`: Bundle format version (currently "1")
- `git_sha`: Exact 40-character git SHA
- `git_dirty`: Boolean indicating if working tree was dirty
- `started_at`, `finished_at`: UTC timestamps (ISO 8601)
- `steps`: Array of step results (see below)
- `verdict`: One of `CERTIFIED`, `INCOMPLETE`, or `FAILED`

Step result schema:
```json
{
  "step_id": "test_clean",
  "name": "Test suite (clean shell)",
  "status": "PASS",  // or "FAIL", "SKIPPED", "INCOMPLETE"
  "reason": "2984 passed, 1 skipped",
  "duration_seconds": 485.3,
  "evidence": {"command": ["python", "-m", "pytest", "--junitxml=..."],
               "exit_code": 0, "junit": {"tests": 2985, "passed": 2984,
               "failures": 0, "errors": 0, "skipped": 1, "sha256": "..."}}
}
```

### `environment.json`

Captured environment details:
- `python_version`: Python interpreter version
- `platform`: Operating system and version
- `uv_version`: uv package manager version
- `node_version`: Node.js version (if available)
- `lockfile_sha256`: SHA256 of `uv.lock`
- `env_var_names`: Sorted list of environment variable **names only** (values never recorded)

### `test-summary.json`

Pytest results from both clean and dirty shells:
- Clean shell: `env -i HOME LANG PATH pytest ...`
- Dirty shell: `LLMGATE_AUTH_TOKEN=bogus-cert-dirty pytest ...`

Both shells use the same controlled `HOME`, `LANG`, and `PATH` environment; the dirty shell adds only the bogus `LLMGATE_AUTH_TOKEN`. Ambient variables are not passed through. Each result records the command, exit code, JUnit totals, a map of testcase IDs to outcomes, and the raw report SHA256. Certification compares testcase IDs and outcomes as well as totals. Missing, malformed, duplicate, or internally inconsistent JUnit evidence makes an otherwise successful pytest run `INCOMPLETE`. Temporary JUnit files are not copied into the bundle.

### `lint-type-build.json`

Results from:
- `ruff check`: command + exit code (no issue count parsed)
- `ruff format --check`: exit code
- `mypy --strict verdict`: exit code + file count
- `uv build`: command + exit code + artifact names + full SHA256 checksums from a fresh build directory. A successful command without a wheel is `INCOMPLETE`.

### `package-smoke.json`

Fresh venv install test:
- Created temporary venv
- Installed built wheel
- Ran `verdict --help`
- Each command and exit code, wheel SHA256, and whether help output contained `usage:`. The selected wheel must come from this invocation; stale wheels are not accepted.

### `security-summary.json`

Security scan results from two tools (both are declared dev dependencies):

1. **bandit** (SAST): Static analysis security testing
   - Command: `bandit -c pyproject.toml -r verdict -ll -f json`
   - Policy: Configured in `[tool.bandit]` in `pyproject.toml`
   - Severity filter: `-ll` limits to MEDIUM and HIGH findings
   - Missing binary: `FAIL` (declared dependency, must be present)

2. **pip-audit** (CVE scan): Dependency vulnerability scanning
   - Command: `pip-audit --local --skip-editable -f json`
   - Checks installed packages against known CVE database
   - Network/DB errors: `INCOMPLETE` (not `FAIL`)
   - Missing binary: `FAIL` (declared dependency, must be present)

The security report includes both commands, exit codes, report SHA256 digests, medium+ Bandit findings, and pip-audit vulnerability count when available. Reports stay temporary; no full scanner output or environment values are stored. A nonzero scanner exit with no parsed findings cannot be considered a pass.

**Unified Policy**: The same bandit configuration is used in:
- CI workflow (`.github/workflows/ci.yml` security job)
- Scheduled security scan (`.github/workflows/security.yml`)
- Release certification (`scripts/certify_release.py` step_security)

This ensures no drift between environments

### `docs-check.json`

Documentation validation:
- `scripts/check_doc_links.py`: command and exit code
- No link count is parsed from this checker; absence of the script marks the step `SKIPPED` and verdict `INCOMPLETE`.

### `git-clean.txt`

Output of `git status --porcelain` after all certification steps.

Must be empty for `CERTIFIED` verdict.

### `rehearsals/`

*(Optional, requires `--rehearsal` arguments)*

For each provided rehearsal (e.g., `clean`, `chaos`):

```
rehearsals/<name>/
  events.jsonl      # Copied from run directory
  receipt.json      # Copied from run directory
  graph.json        # Graph needed to rebuild the receipt
  review.json       # Copied if present
```

Both `clean` and `chaos` runs are required. The verifier rebuilds the receipt
from graph, events and review; checks the claimed and recomputed outcome is
`COMPLETE`; rejects injected faults in `clean`; and requires a recorded injected
failure in `chaos`. It records the claimed executed-model identities and reviewer
when available. Invalid JSON, missing files, failed runs and forged digest-only
receipts are `FAIL`. Missing either proof is `INCOMPLETE`.

The current verifier **does not independently attest to the run producer**.
The runtime's own files can be self-consistent without proving a real gateway
execution. Thus even valid local rehearsals are `INCOMPLETE`, never `CERTIFIED`.
An independently verifiable producer attestation must be added before that verdict
can be issued. No rehearsals also means `SKIPPED` and `INCOMPLETE`.

### `CERTIFICATION.md`

**Generated from JSON files only.** Human-readable summary containing:
- Verdict and git SHA
- Environment snapshot
- Step results table
- Normalization notes

**Important**: This file is generated from the JSON data. Never edit counts or status manually.

## Verdict Semantics

### `CERTIFIED`

All conditions met:
- Git working tree is clean (`git_dirty: false`)
- All eleven required steps are `PASS` and required step evidence exists
- Clean and controlled-failure rehearsals were independently attested (not yet supported)
- No step returned `FAIL`

### `INCOMPLETE`

At least one of:
- Git working tree is dirty (`git_dirty: true`)
- Rehearsals were skipped (no `--rehearsal` provided)
- Any step is `SKIPPED` or `INCOMPLETE` (including missing documentation checker or evidence)

A `FAIL` takes precedence over `INCOMPLETE`.

### `FAILED`

At least one step returned `FAIL`.

## Normalized Fields

The following fields vary between runs on identical inputs and are explicitly normalized:

- **Timestamps**: `started_at`, `finished_at`, and all `duration_seconds` values
- **Temporary paths**: Any temp directories created during testing
- **Environment variable list**: OS-dependent; may differ between platforms

Evidence includes normalized temporary command paths (JUnit, build, security, and smoke). Raw report SHA256 hashes, package digests, host metadata, scanner data, and external rehearsal inputs can vary even for the same source SHA. Do not assume byte-identical bundles or deterministic verdicts from the SHA alone. Testcase ID/outcome parity is checked only between the two controlled shells of one run.

## CI Integration

The current CI push lane creates a cheap source receipt; it **does not run the
full certification** or upload a full certification bundle on each push. The
manual evidence generator may produce an `INCOMPLETE` bundle; this is not a
`CERTIFIED` release gate. A complete certification workflow is not installed.

`ci-job.yml` is a reference example, not the active push workflow. Do not
interpret a source receipt or a manual `INCOMPLETE` artifact as certification.

## Pointing to Latest Certification

**Release certification acceptance criterion**: README and cheat sheet should point to the latest certified SHA instead of copying counts.

### In README.md

Replace static test counts with a pointer:

```markdown
See the latest certification bundle: [artifacts/certification/<sha>/CERTIFICATION.md]
```

### In docs/cheat-sheet.md

Add a single-line reference:

```markdown
Latest certification: <sha> (see artifacts/certification/<sha>/)
```

## Rehearsal Workflow

To include rehearsals in certification:

1. **Run clean rehearsal**:
   ```bash
   verdict orchestrate "..." --repo . --runs-dir clean-run/
   ```

2. **Run chaos rehearsal**:
   ```bash
   verdict orchestrate "..." --repo . --inject "*=quota" --runs-dir chaos-run/
   ```

3. **Certify with rehearsals**:
   ```bash
   python scripts/certify_release.py \
     --rehearsal clean=clean-run \
     --rehearsal chaos=chaos-run
   ```

4. **Verify verdict**:
   ```bash
   jq -r '.verdict' artifacts/certification/<sha>/manifest.json
   ```

Expected today: `INCOMPLETE` even if all steps pass, because local receipts do not independently attest to their producer.

## Schema Evolution

When the bundle schema changes:
1. Increment `schema_version` in `CertificationManifest`
2. Document changes in this README under a new "Schema v2" section
3. Ensure backward compatibility in parsing tools or provide migration scripts

## Security: No Secret Leakage

**Critical invariant**: Environment variable **values** are never written to certification bundles.

Only variable **names** are captured in `environment.json:env_var_names`.

This is validated by test: `test_environment_no_secret_values`.

## Example: Reading a Bundle

```python
import json
from pathlib import Path

bundle_dir = Path("artifacts/certification/<sha>")

# Load manifest
manifest = json.loads((bundle_dir / "manifest.json").read_text())
print(f"Verdict: {manifest['verdict']}")
print(f"SHA: {manifest['git_sha']}")

# Check step results
for step in manifest["steps"]:
    print(f"{step['name']}: {step['status']}")
    if step['reason']:
        print(f"  → {step['reason']}")

# Load environment
env = json.loads((bundle_dir / "environment.json").read_text())
print(f"Python: {env['python_version']}")
print(f"Platform: {env['platform']}")
```

## Troubleshooting

### Certification fails with "Git working tree is dirty"

**Solution**: Commit or stash changes, or use `--allow-dirty` (forces `INCOMPLETE`).

### Missing rehearsals

**Symptom**: Verdict is `INCOMPLETE`, rehearsal step shows `SKIPPED`.

**Solution**: Provide `--rehearsal` arguments pointing to completed run directories.

### Step fails: "bandit not available"

**Solution**: Add `bandit` to `[project.optional-dependencies.dev]` in `pyproject.toml` or accept `SKIPPED` status.

### mypy --strict fails

**Solution**: Fix type errors in `verdict/` source. This is a blocking failure.

### Tests fail in clean vs dirty shell

**Symptom**: Different pass/fail counts between clean and dirty runs.

**Impact**: Certification `INCOMPLETE` (or `FAILED` if a test itself fails). This indicates environment-dependent test behavior.

**Solution**: Fix tests to be hermetic and not rely on ambient environment variables.

## References

- Release certification generator: `scripts/certify_release.py` (tests: `tests/test_certify_release.py`)
- Original manual certification matrix (historical; superseded by the generated evidence bundles above)
- Runtime certification (different feature, runtime component detection): `verdict/runtime_certification.py`
