# Quickstart: Validating the Launch Gate

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

Validates SC-001 through SC-005 from a clean checkout. Matches FR-008: every
evidence artifact listed below must be reproducible locally, not only inside
CI.

## Prerequisites

- Python 3.10+, `uv` installed
- Node 20+, `npm` installed (for the client/contracts package's SBOM +
  existing `node-security` job)
- Docker (or an equivalent local container runtime) — required for the
  dynamic check's isolated local process, per FR-003
- Clean checkout of this feature branch

```bash
uv sync --extra dev --extra server --extra dashboard
npm ci
```

## 1. Existing blocking gates (baseline — unchanged by this feature)

```bash
uv run pytest -q
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
```

Expected: all pass, matching CLAUDE.md's existing baseline. This feature does
not change these commands; it adds the stages below alongside them.

## 2. SBOM generation (FR-001)

```bash
uv run cyclonedx-py environment -o sbom-python.cdx.json
npx @cyclonedx/cyclonedx-npm --output-file sbom-node.cdx.json
```

Expected: two CycloneDX JSON files are produced, each validating against the
`sbomArtifact` shape in
[`contracts/launch-gate-evidence.schema.json`](./contracts/launch-gate-evidence.schema.json).
A tool error (non-zero exit) must be treated as a blocking failure, not a
skipped step (spec.md acceptance scenario 2).

## 3. Provenance attestation (FR-002)

Provenance generation is CI-only (it requires GitHub's OIDC token and the
Sigstore/Fulcio signing flow — there is no meaningful local equivalent).
Validate a published release's attestation instead:

```bash
gh attestation verify <artifact-path-or-oci-ref> --owner <org>
```

Expected: verification succeeds and reports the source revision and build
workflow, matching the `provenanceAttestation` shape in the evidence schema.

## 4. Dynamic check (FR-003)

```bash
uv run --extra server uvicorn verdict.api:app --host 127.0.0.1 --port 8000 &
docker run --rm --network=host -v "$(pwd):/zap/wrk:rw" -t zaproxy/zap-stable \
  zap-baseline.py -t http://127.0.0.1:8000 -J zap-report.json
```

Expected: the scan runs against the locally-started process (never a staging
URL — FR-003), and reports zero unwaived critical/high findings. If the
server process fails to start, this is a blocking failure
(`dynamic_check.status = "target_failed_to_start"`), not a skipped stage.

## 5. Memory/learning PII and secret boundary tests (FR-005)

```bash
uv run pytest tests/security/ -v
```

Expected: one test module per `verdict/memory_*` boundary (see research.md
§4); each asserts synthetic PII/secret fixtures are not retrievable from an
unauthorized scope. A boundary with no corresponding test file is a coverage
gap against SC-004 and must be treated as a blocking finding, not silently
passed.

## 6. Retention/erasure and telemetry consent (FR-006, FR-007)

```bash
uv run pytest tests/privacy/ -v
```

Expected: the erasure-simulation test confirms data is unreachable within the
30-day GDPR-equivalent SLA
([`data-model.md#retentionerasurepolicyrecord`](./data-model.md#retentionerasurepolicyrecord)),
and the telemetry-consent tests confirm zero transmission in the opt-out
state and expected transmission only in the opt-in state.

## 7. Full evidence set (SC-003)

A reviewer with no prior context should be able to run steps 1–6 above and
assemble the results into a `LaunchGateEvidenceRecord` matching
[`contracts/launch-gate-evidence.schema.json`](./contracts/launch-gate-evidence.schema.json)
in under 30 minutes from a clean checkout. If any step's tool is missing or
network-unreachable, that step's result is `unavailable`, not `pass`
(FR-009) — do not report success for a step that could not run.
