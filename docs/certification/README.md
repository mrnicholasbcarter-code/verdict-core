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

# Run with actual child run directories (each contains graph/events/receipt)
python scripts/certify_release.py \
  --rehearsal clean=/path/to/clean-runs/<clean-run-id> \
  --rehearsal chaos=/path/to/chaos-runs/<chaos-run-id>

# Allow a dirty preflight (the final dirty check still produces FAILED)
python scripts/certify_release.py --allow-dirty
```

### Output Location

Certification bundles are written to:

```
artifacts/certification/<sha>/
```

Where `<sha>` is the current git HEAD SHA. A previously published bundle is **not** removed when a new same-SHA attempt starts, fails preflight, times out, or crashes. Each attempt has a separate machine-readable status at `artifacts/certification/.attempts/<sha>/<utc-attempt-id>.json` with a status, reason, and start/finish times. An interrupted attempt has `ABORTED` and a null finish time until the process finishes; read attempt records to distinguish the most recent attempt from the retained bundle. A failed/incomplete same-SHA rerun with an existing bundle stores its full generated evidence at the attempt record's `bundle_path` (`.attempts/<sha>/<attempt-id>.bundle/`) and does not overwrite the old bundle. A complete bundle is built in a same-filesystem staging directory. A successful replacement moves the previous bundle to `artifacts/certification/.history/<sha>/<attempt-id>/` before renaming the new bundle into the unchanged published layout. There is a brief interval in which the public path is absent, but the previous bundle already exists in history. The runner checks HEAD before and after each step and again just before publish; if HEAD changes, it aborts. Custom `--output-dir` locations must be empty and their existing files are never deleted. The default directory must be ignored and contain no tracked files; symlinked output targets are refused. An independent producer verifier is still missing; this change does not produce a `CERTIFIED` verdict.

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
   - Recognized network/DB errors from a completed process with no report: `INCOMPLETE`; process timeouts, OS errors, or unmatched scanner errors can be `FAIL`
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
when available, including per-attempt `node_id`, `attempt`, `intended_route`,
`executed_model`, `provider`, `route_id`, `route_identity`, `outcome`, `fault_injected`, and
`failure_category`. Those fields are explicitly labeled **receipt-reported, not
independently attested**. Invalid JSON, missing files, failed runs and forged digest-only
receipts are `FAIL`. Missing either proof is `INCOMPLETE`.

Local receipts **do not independently attest to the run producer**. They remain
`INCOMPLETE`, never `CERTIFIED`. No rehearsals means `SKIPPED` and `INCOMPLETE`.
The optional `--attested-rehearsals <dir>` path requires both `clean/` and `chaos/`,
each with `receipt.json`, `events.jsonl`, `graph.json`, and `attestation.json`.
The signed bundle is retained beside the copied evidence. This path also verifies
GitHub OIDC/Sigstore provenance before it can pass; see [attested CI](#attested-ci-rehearsals).

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
- Clean and controlled-failure rehearsals passed independent GitHub OIDC attestation verification
- No step returned `FAIL`

### `INCOMPLETE`

If no step fails, at least one missing or partial condition causes `INCOMPLETE`, such as
skipped rehearsals or an `INCOMPLETE`/`SKIPPED` step. The verdict computation also
considers `git_dirty`, but the final git-clean step marks remaining dirt `FAIL`.
`--allow-dirty` only bypasses the initial dirty-tree rejection; it cannot certify
and does **not** guarantee `INCOMPLETE`. Any `FAIL` takes precedence.

### `FAILED`

At least one step returned `FAIL`.

## Normalized Fields

The following fields can vary between runs on identical inputs:

- **Timestamps and durations**: `started_at`, `finished_at`, and `duration_seconds`
  remain in the bundle; exclude them when comparing attempts.
- **Temporary command paths**: Checkout and temporary command paths in step
  evidence (JUnit, lint, type, build, security, smoke, and docs) are normalized.
- **Environment variable names**: The sorted `env_var_names` list is retained,
  but its contents can vary across hosts; exclude it from cross-host comparisons.

Evidence includes normalized checkout and temporary command paths (JUnit, lint, type, build, security, smoke, and docs). Subprocesses have finite per-command timeouts; timeout/error class metadata accompanies a failed step without copying secrets or checkout paths into the report. Raw report SHA256 hashes, package digests, host metadata, scanner data, and external rehearsal inputs can vary even for the same source SHA. Do not assume byte-identical bundles or deterministic verdicts from the SHA alone. Testcase ID/outcome parity is checked only between the two controlled shells of one run.

## CI Integration

The current CI push lane creates a cheap source receipt; it **does not run the
full certification** or upload a full certification bundle on each push. The
manual evidence generator produces `INCOMPLETE` when no rehearsal run is given.
With independently attested clean/chaos evidence, it can produce `CERTIFIED`.
This does not enable the release gate: `VERDICT_REQUIRE_CERTIFIED_BUNDLE` remains
an operator-controlled repository variable. Source receipts and `INCOMPLETE`
artifacts never satisfy that gate.

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

These commands are a **live** recipe, not a claim that this checkout contains
successful rehearsals. Use separate disposable git repositories for worker edits,
keep run/state files outside the certification checkout, and confirm the checkout
is clean before certification. Supply gateway credentials, available admitted
routes, and OCR as required for the selected live setup. Run each command from
the verdict-core root; replace paths and goals with real values.

1. **Run the clean rehearsal and record its printed `run:` path**:
   ```bash
   verdict orchestrate "<clean goal>" --repo /path/to/disposable-clean-repo \
     --runs-dir /path/to/isolated-runs/clean
   ```

2. **Run worker-scoped chaos with an explicit isolated health state file;
   record its printed `run:` path**:
   ```bash
   verdict orchestrate "<chaos goal>" --repo /path/to/disposable-chaos-repo \
     --runs-dir /path/to/isolated-runs/chaos \
     --state-file /path/to/isolated-runs/chaos-health.json \
     --inject "worker#1=route_quota"
   ```
   Without a prebuilt `--graph`, `*=quota` can be consumed by planning rather
   than a worker. Verify that the clean and chaos runs both report `COMPLETE`,
   and that the chaos receipt records an **injected worker-attempt failure**.
   A recoverable fault is not a guarantee of completion: stop if either run is
   blocked. Use unique state files for separate chaos rehearsals.

3. **Pass the actual child run directories**, not their `--runs-dir` parents.
   The CLI prints `run: <runs-root>/<run-id>`; set the variables to those
   exact printed paths and check each receipt before certifying:
   ```bash
   CLEAN_RUN="/path/to/isolated-runs/clean/<printed-clean-run-id>"
   CHAOS_RUN="/path/to/isolated-runs/chaos/<printed-chaos-run-id>"
   verdict run-receipt "$CLEAN_RUN"
   verdict run-receipt "$CHAOS_RUN"
   python scripts/certify_release.py \
     --rehearsal "clean=$CLEAN_RUN" \
     --rehearsal "chaos=$CHAOS_RUN"
   ```
   Each directory must contain `graph.json`, `events.jsonl`, and `receipt.json`
   directly. A valid local receipt is not independent producer attestation.

4. **Inspect the generated attempt and published bundle paths printed by the
   command**. For an existing same-SHA bundle, an incomplete rerun may be
   retained only at the attempt record's `bundle_path`, not at the published
   path. Read that bundle's `manifest.json` verdict. Expected today:
   `INCOMPLETE` **only if no step fails**; local receipts cannot independently
   attest to their producer. A dirty checkout or other failed step yields
   `FAILED`.

### Attested CI rehearsals

1. An operator dispatches `.github/workflows/certify-rehearsal.yml` on `main`.
   The job uses the protected `certification` environment. Its secrets are
   `VERDICT_CERT_GATEWAY_URL` and `VERDICT_CERT_GATEWAY_KEY`. Environment approval
   and a scoped gateway key are prerequisites; this feature does not create them.
2. The job checks out the exact event SHA and syncs frozen dependencies. It builds
   two isolated fixture repositories. It uses direct-gateway workers and a pinned,
   SHA256-checked OCR binary. All model selection is narrowed to `free,subscription`
   in canonical admission. Workers have four attempts per node, 300-second attempt
   timeouts, and a 1500-second run deadline. An outer 1800-second timeout bounds each
   run. Chaos injects planning quota/no-final-answer and worker rate-limit faults.
3. Both receipts must be `COMPLETE`, pass `verify_run_receipt`, and record the exact
   checkout SHA. The workflow signs each receipt with `actions/attest-build-provenance`.
   It uploads run evidence and signed bundles as `certify-rehearsals-<sha>-<run_id>`.
   Known gateway secrets are masked and checked in run files before upload. This is
   not a claim that arbitrary sensitive content is detectable.
4. Dispatch `certification.yml` at the **same main SHA** with `rehearsal_run_id`.
   It downloads that named artifact and runs the certifier with the attested input.
   Eleven `PASS` steps and exit 0 are required to retain
   `certification-evidence-certified-<sha>-<run_id>-<attempt>`, as the release gate expects.
   Omitting the input keeps the existing `INCOMPLETE` path and exact exit-1 check.

For a downloaded artifact, run from its exact clean checkout:

```bash
python scripts/certify_release.py --attested-rehearsals /path/to/downloaded-artifact
```

The independent verifier calls `gh attestation verify` with the local bundle,
the pinned repository `mrnicholasbcarter-code/verdict-core`, signer workflow
`mrnicholasbcarter-code/verdict-core/.github/workflows/certify-rehearsal.yml`,
`refs/heads/main`, the exact source and signer SHA, GitHub's OIDC issuer,
SLSA provenance v1, and a ban on self-hosted runners. Only exit 0 and parsed
verified statements proceed. Every subject must have the exact receipt SHA256.
The receipt producer SHA must match exactly; the local-path `verdict/`-unchanged
relaxation does **not** apply. Both semantic checks and clean/chaos fault checks
still run. Missing bundles or an unavailable executable yield `INCOMPLETE`.
Signature, policy, digest, SHA, timeout, malformed output, or semantic failures
are `FAIL`. The injectable verifier lets tests cover these paths without gh or
network access. Serialized evidence flags alone cannot mint certification.

The independence boundary is **GitHub OIDC plus Sigstore**, not the receipt's own
claim about its producer. It attests that the named main workflow at the named
SHA produced these receipt bytes. It does not prove that a model actually ran,
that its answer is correct, or that the gateway reported truthful identities.
The gateway and providers remain operator-controlled. Rehearsal content remains
produced by Verdict. See the [receipt threat model](../THREAT_MODEL_RECEIPTS.md#independent-rehearsal-attestation).

## Schema Evolution

When the bundle schema changes:
1. Increment `schema_version` in `CertificationManifest`
2. Document changes in this README under a new "Schema v2" section
3. Ensure backward compatibility in parsing tools or provide migration scripts

## Security: No Secret Leakage

The generated environment snapshot `environment.json:env_var_names` captures
only environment variable **names**, not values. This narrow behavior is
checked by `test_environment_no_secret_values`. It does **not** establish that
all certification bundle content is secret-free: supplied rehearsal
`graph.json`, `events.jsonl`, `receipt.json`, and optional `review.json` are
copied verbatim. Sanitize those inputs before bundling; do not put secret values
in free-text fields. A full-bundle leak scan is needed before promising no
values anywhere.

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

**Solution**: Commit or stash changes. `--allow-dirty` bypasses the initial preflight only; final dirt makes the git-clean step `FAIL` and the verdict `FAILED`.

### Missing rehearsals

**Symptom**: Verdict is `INCOMPLETE`, rehearsal step shows `SKIPPED`.

**Solution**: Provide `--rehearsal` arguments pointing to completed run directories.

### Step fails: "bandit not available"

**Solution**: Run `uv sync --frozen --extra dev --extra server`; Bandit is already a declared dev dependency, and a missing binary is blocking `FAIL`.

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
