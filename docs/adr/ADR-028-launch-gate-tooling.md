# ADR-028: Security and privacy launch-gate tooling

- **Status:** Accepted — implemented on `feat/238-launch-001`
- **Date:** 2026-09-04
- **Deciders:** Verdict Core maintainers
- **Related:** [ADR-012](ADR-012-consented-budgeted-probes.md), [ADR-017](ADR-017-durable-privacy-safe-receipt-ledger.md), [ADR-020](ADR-020-gateway-adapter-contracts.md)

## Context

The release pipeline already runs dependency, secret, static-analysis, and CodeQL checks,
but a release also needs evidence about its complete dependency inventory, build origin, and
running HTTP surface. Memory and learning boundaries additionally need explicit privacy tests,
and retention and telemetry behavior needs a reproducible release-gate record. The gate must
fail closed: a missing, failed, or unavailable check cannot become a pass through omission.

The implementation must remain reproducible from a clean checkout, avoid scanning a staging
system, and avoid adding static credentials or publishing scanner findings as GitHub issues.

## Decision

1. **SBOM:** Generate CycloneDX 1.6 JSON independently for the Python and Node ecosystems.
   Python uses the documented `cyclonedx-py environment` command after the `uv` environment is
   installed. Node uses the pinned `@cyclonedx/cyclonedx-npm` generator with dev dependencies
   omitted. Each output is retained as a CI artifact, and failed generation is represented as
   failed evidence rather than omitted.
2. **Provenance:** Use GitHub's `actions/attest-build-provenance@v2` for Python distributions
   and npm trusted publishing provenance for Node packages. The release workflow records the
   artifact digest, source revision, build environment, predicate type, and attestation
   reference in a machine-readable evidence artifact. No repository-held signing key is added.
3. **Dynamic check:** Use `zaproxy/action-baseline@v0.15.0` against `http://127.0.0.1:8000`,
   where `verdict.api:app` runs as an ephemeral CI process. Set `fail_action: true` and
   `allow_issue_writing: false`; the scan is never pointed at a staging deployment and cannot
   open a GitHub issue as a side effect. A failed health check is an explicit blocking
   `target_failed_to_start` outcome.
4. **Memory/privacy evidence:** Run the security and privacy suites as blocking pytest
   steps with JUnit XML artifacts. Convert reports through bounded, fail-closed helpers in
   `verdict.release.evidence`: missing, malformed, unsafe, incomplete, failed, errored, and
   skipped reports do not produce a pass. Boundary tests cover the gate, plane, bridge, and
   every current memory adapter. Known redaction gaps remain visible as strict xfails until
   separately remediated.
5. **Telemetry consent:** Observability sinks default to no emission. A caller must pass
   `consent_given=True` to write telemetry; emitted records remain restricted to operational
   fields and undergo sensitive-value redaction.
6. **Waivers:** A waiver is an append-only, attributed record validated by
   `verdict.release.waivers`. Per-finding waivers identify one finding; infrastructure-outage
   waivers require a registered emergency approver. The waiver CLI refuses to overwrite an
   existing evidence file and validates the complete input record before writing a new one.

## Consequences

- Reviewers receive one reproducible evidence vocabulary across supply-chain, dynamic,
  memory-boundary, retention, and telemetry checks.
- CI artifacts are preserved even when a scan or test fails, while the corresponding gate
  remains blocking and cannot silently pass because a report is absent.
- ZAP adds passive runtime coverage without a staging deployment or issue-writing side effect.
- The local telemetry default is privacy-preserving, but existing integrations that want
  telemetry must now make consent explicit.
- The current memory-gate bearer/basic-token and generic-PII redaction gaps are deliberately
  surfaced by strict xfail tests; this ADR does not claim those production defects are fixed.
