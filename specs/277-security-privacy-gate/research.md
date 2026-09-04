# Phase 0 Research: Cross-Repository Security and Privacy Launch Gate

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

All items below were previously flagged in spec.md's Assumptions as
planning-phase (not scope-defining) decisions. Each is resolved here after
reading the tool's own docs/README (per workspace Rule #1), not inferred from
its name.

## 1. SBOM format and generator

- **Decision**: CycloneDX, generated via `cyclonedx-bom` (CLI: `cyclonedx-py`)
  for the Python side, invoked in `environment` mode against the `uv`-managed
  virtualenv; CycloneDX's npm generator (`@cyclonedx/cyclonedx-npm`) for the
  Node/client package side. One SBOM per ecosystem, both CycloneDX so
  downstream tooling (and REL-001's future cross-repo aggregation) only needs
  to parse one format.
- **Rationale**: `cyclonedx-bom`'s documented `environment` input mode scans
  packages already installed in the current interpreter/venv — this matches
  `uv`-managed installs directly (`uv sync` then run the scanner against that
  venv) with no lockfile-format translation needed. CycloneDX is also already
  the format `osv-scanner` and most supply-chain tooling in this repo's
  ecosystem consume natively. Using the same SBOM family for both `uv.lock`
  (Python) and `package-lock.json` (Node) — the two lockfiles `osv-security`
  already scans — keeps the new SBOM stage aligned with the existing
  `osv-security` job's inputs rather than introducing a third lockfile
  parser.
- **Alternatives considered**: SPDX (equally valid industry standard; rejected
  only because CycloneDX's `environment`-mode Python generator requires no
  Poetry/Pipenv-specific lockfile parsing, which better matches this repo's
  `uv`-only Python dependency management — the project's `pyproject.toml` does
  not use Poetry). Direct `pip-audit --format=cyclonedx-json` output alone was
  considered but rejected as not covering the Node side, and this feature
  needs one report format across both ecosystems.

## 2. Build provenance attestation

- **Decision**: `actions/attest-build-provenance`, GitHub's own action for
  producing SLSA-aligned, in-toto-format signed provenance statements bound
  to the release artifact's SHA-256 digest, published via GitHub's native
  attestation API (Sigstore-backed: Fulcio + Rekor).
- **Rationale**: This repo's entire CI/CD surface is already GitHub Actions
  (`security.yml`, `codeql.yml`, `ci.yml`, `release.yml`); a GitHub-native
  attestation avoids standing up a separate signing/transparency-log
  infrastructure. Attestations are queryable and verifiable after the fact
  via `gh attestation verify`, satisfying FR-002's "recording source revision
  and build environment" and FR-008's reproducibility-from-clean-checkout
  requirement.
- **Alternatives considered**: A hand-rolled in-toto attestation signed with a
  repo-held key was rejected — it would require managing and rotating a
  signing key ourselves, a strictly weaker security posture than GitHub's
  short-lived (10-minute) Fulcio-issued certificates. Note: GitHub's docs mark
  `attest-build-provenance` (v4+) as a thin wrapper over the newer
  `actions/attest`; implementation should pin whichever is current at
  build time and re-verify the action's own README before pinning a version.

## 3. Dynamic (runtime) security check

- **Decision**: `zaproxy/action-baseline` (OWASP ZAP baseline scan) run in CI
  against the built artifact's `server` extra (FastAPI/uvicorn) started
  locally inside the same CI job — i.e., the "isolated local/sandboxed
  container or process" the spec's FR-003/Assumptions require, addressed as
  `http://127.0.0.1:<port>`, never a staging deployment.
- **Rationale**: `action-baseline`'s target parameter is documented to accept
  a locally accessible URL, not only a public one, which is exactly the local
  ephemeral-process model this feature's clarification session settled on.
  ZAP baseline is a passive scan (safe to run against a fresh in-CI process
  with no external traffic) and produces a standard report artifact,
  consistent with FR-008's reproducibility requirement.
- **Alternatives considered**: `zaproxy/action-full-scan` (active scan) was
  rejected for the default gate — an active scan is heavier and more
  appropriate for a scheduled/optional deeper pass than a per-release blocking
  gate; it can be added later as a non-blocking supplement without changing
  this decision. Container-image scanning (e.g., Trivy) was considered but
  rejected as a *substitute* for a dynamic check — it inspects the image's
  static contents (closer to SBOM/dependency scanning, already covered by
  `osv-security`), not the running artifact's actual exposed runtime surface,
  which is what FR-003 requires.

## 4. PII/secret boundary test approach (memory/learning subsystems)

- **Decision**: A dedicated `tests/security/` module set, one test file per
  memory/learning boundary module found in `verdict/` (`memory_gate.py`,
  `memory_plane.py`, `memory_bridge.py`, and each `memory_*_adapter.py`),
  each asserting that synthetic PII- and secret-shaped fixtures written
  through the boundary are not retrievable from an unauthorized scope/query.
  Added to the existing blocking `uv run pytest -q` invocation already
  required by CLAUDE.md's baseline verification, and to CI as a required job.
- **Rationale**: Following the existing pattern in
  `tests/test_execution_packet_security.py` keeps the new tests idiomatic to
  this repo rather than inventing a new test framework or runner. One file per
  boundary module makes FR-005/SC-004's "100% of code paths... covered"
  requirement directly auditable (file-for-file against the `verdict/memory_*`
  module list).
- **Alternatives considered**: A single monolithic
  `tests/test_memory_pii_boundaries.py` covering all adapters was rejected —
  it would obscure per-boundary coverage gaps and make SC-004's 100%-coverage
  claim harder to verify by inspection.

## 5. Retention/erasure and telemetry-consent verification

- **Decision**: Policy documented under `docs/privacy/retention-erasure.md`
  and `docs/privacy/telemetry-consent.md`; each backed by an automated test
  under `tests/privacy/` — an erasure-request simulation asserting removal
  within the 30-day GDPR-equivalent SLA (FR-006), and telemetry on/off-state
  tests asserting zero transmission when consent is not granted (FR-007).
- **Rationale**: Matches the spec's Independent Test for User Story 3
  directly (simulate erasure, toggle consent, assert both automatically) and
  keeps policy prose and its enforcement test co-located by topic rather than
  scattered.
- **Alternatives considered**: Folding retention/consent docs into the
  existing top-level `docs/adr/` ADR series alone (no separate policy doc) was
  rejected — an ADR records a decision's rationale, not an evolving,
  user-facing policy statement; this feature needs both (an ADR for *why* 30
  days/GDPR-equivalent was chosen, already recorded in spec.md's
  Clarifications, plus a standing policy doc for FR-007's "document its
  telemetry consent behavior").

## Outstanding items for Phase 1 / implementation

None of the above leaves a `NEEDS CLARIFICATION` in Technical Context — all
five decisions are made. Implementation should re-confirm current action/tool
versions against their own READMEs immediately before pinning versions in
`security.yml`, since this research was done at plan time (2026-09-04) and
these are fast-moving supply-chain tools.
