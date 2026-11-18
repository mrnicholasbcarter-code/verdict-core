#!/usr/bin/env python3
"""
Update CLAIMS.md to add test evidence paths to shipped claims.
"""

from pathlib import Path

CLAIMS_PATH = Path(__file__).parent.parent / "docs" / "CLAIMS.md"

# Map of claim_id -> test file(s) to add
TEST_EVIDENCE_MAP = {
    # C-002: "Model admission, bounded recovery, a separate review route and receipts you can verify"
    "C-002": "tests/test_orch_recovery.py, tests/test_orch_review.py, tests/test_proof_receipts.py",
    # C-004: "Uses eligible free and already-paid capacity first"
    "C-004": "tests/test_admission.py, tests/test_intelligence.py",
    # C-008: "Asks before risky actions"
    "C-008": "tests/test_admission.py, tests/test_actions_cli_wiring.py",
    # C-010: "verdict eligibility --verified shows each listed model's evidence-backed status"
    "C-010": "tests/test_actions_cli_wiring.py, tests/test_verified_models_projection.py",
    # C-011: "/eligibility refresh shows bounded request plan grouped by capacity class"
    "C-011": "tests/test_admission.py, tests/test_actions_cli_wiring.py",
    # C-012: "verdict harness prime select/restore previews change, asks confirm, backs up file"
    "C-012": "tests/test_harness_prime.py, tests/test_harness_bootstrap_surfaces.py",
    # C-013: "/bootstrap claude gives honest Claude Code compatibility report"
    "C-013": "tests/test_harness_claude.py, tests/test_harness_bootstrap_surfaces.py",
    # C-014: "Smart Tab-completion in the prompt"
    "C-014": "tests/test_tui_completion.py, tests/test_tui_completion_surfaces.py",
    # C-017: "Free capacity still needs fresh admission evidence"
    "C-017": "tests/test_admission.py",
    # C-018: "No cost saving is claimed yet" - this is a negative claim, not really testable
    # Keep as is - it's a statement about what is NOT claimed
    # C-019: "Live orchestration requires a gateway" - limitation, not a feature
    # Keep as is
    # C-022: "Receipt integrity covers event logs, not LLM or OCR output"
    "C-022": "tests/test_proof_receipts.py",
    # C-023: "No static fallback chains; OpenTelemetry is optional"
    "C-023": "tests/test_orch_recovery.py, tests/test_tracing.py",
    # C-024: "Version 0.5.0, active development" - metadata, not testable
    # Keep as is
    # C-025: "pip install verdict-core" - has CI workflow test
    "C-025": ".github/workflows/fresh-install.yml",
    # C-026: "verdict detect --json"
    "C-026": "tests/test_actions_cli_wiring.py",
    # C-027: "verdict probe task-coding --base-url ... --allow-live-probe --json"
    "C-027": "tests/test_actions_cli_wiring.py",
    # C-033: "Gate composes eligibility + intelligence"
    "C-033": "tests/test_admission.py",
    # C-038: "Dispatcher binds authorized selected_route"
    "C-038": "tests/test_dispatcher.py",
    # C-041: "Orchestration layer (ADR-036): shipped verdict.orchestration control plane"
    "C-041": "tests/test_orch_runtime.py, tests/test_orch_recovery.py, tests/test_orch_review.py",
    # C-042: "Request routing: decision and execution are separate"
    "C-042": "tests/test_api_dispatch_permits.py, tests/test_orch_runtime.py",
    # C-043: "Explainability: /v1/route/explain returns freshness + eligibility explain records"
    "C-043": "tests/test_api_dispatch_permits.py, tests/test_availability_cache.py",
    # C-046: "Performance: No CI-backed latency SLA is currently claimed" - negative claim
    # Keep as is
    # C-047: "Installed-runtime provenance and maintenance refresh lane"
    "C-047": "tests/test_doctor_installed_state.py, tests/test_verified_refresh_consumers.py",
    # C-049: "Example: local gateway" - just an example
    # Keep as is
    # C-050: "Restricted gateway connections snapshots"
    "C-050": "tests/test_gateway_lifecycle.py",
    # C-052: "Three-valued capability states: supported, unsupported, unknown"
    "C-052": "tests/test_capability_passports.py",
    # C-053: "Separate claimed and observed evidence"
    "C-053": "tests/test_capability_passports.py",
    # C-054: "Source, observation time, expiry, confidence, evidence digest, and limitations on every signal"
    "C-054": "tests/test_capability_passports.py",
    # C-055: "Canonical serialization and integrity digest"
    "C-055": "tests/test_capability_passports.py",
    # C-056: "Hard-requirement evaluation that accepts only fresh observed support"
    "C-056": "tests/test_capability_passports.py",
    # C-057: "A catalog claim never satisfies a hard requirement"
    "C-057": "tests/test_capability_passports.py",
    # C-058: "Missing, expired, malformed, or claim-only evidence resolves to unknown"
    "C-058": "tests/test_capability_passports.py",
    # C-059: "Fresh failed observation remains authoritative over a positive catalog claim"
    "C-059": "tests/test_capability_passports.py",
    # C-060: "Passport expiry also makes every capability unknown"
    "C-060": "tests/test_capability_passports.py",
    # C-061: "verdict autodev packet execute --context-output-policy opts in for one run"
    "C-061": "tests/test_autodev_wiring.py, tests/test_cli_present_autodev.py",
    # C-062: "The flag is off by default"
    "C-062": "tests/test_autodev_wiring.py",
    # C-063: "Python packet launch API accepts output_policy=ContextOutputPolicy(enabled=True)"
    "C-063": "tests/test_autodev_wiring.py",
    # C-064: "No shared configuration, HOME storage, provider calls, or tool interception is added"
    "C-064": "tests/test_autodev_wiring.py, tests/test_harness_prime.py",
    # C-065: "The policy observes the raw post-compilation context string"
    "C-065": "tests/test_context_pack.py, tests/test_autodev_context.py",
    # C-066: "Raw passthrough is the only supported transform"
    "C-066": "tests/test_autodev_context.py",
    # C-067: "Compiler budget, security exclusions, and sanitization remain unchanged"
    "C-067": "tests/test_context_pack.py, tests/test_autodev_context.py",
    # C-068: "Existing context receipt gets raw_artifact_pointer, output_policy, and output_metrics"
    "C-068": "tests/test_autodev_context.py, tests/test_autodev_outcome_attempts.py",
    # C-069: "Input/output bytes measure only raw policy passthrough"
    "C-069": "tests/test_autodev_context.py",
    # C-070: "OFF preserves pack bytes, digest, receipt shape, and legacy receipt key"
    "C-070": "tests/test_autodev_context.py",
    # C-071: "Verdict is a control plane for coding work (plan, admit, run, recover, review, receipt)"
    # This is a description - keep as is
    # C-072: "It is not an LLM gateway, and no cost saving is claimed" - negative claim
    # Keep as is
    # C-073: "The receipt ledger is a local, privacy-safe audit boundary"
    "C-073": "tests/test_receipt_store.py, tests/test_proof_receipts.py",
    # C-074: "Decision-time route metadata, lifecycle state, verification status, hashes, and scope identifiers are the canonical assets"
    "C-074": "tests/test_receipt_store.py, tests/test_proof_receipts.py",
    # C-075: "Raw prompts, tool arguments, outputs, credentials, and provider tokens are outside the default storage boundary"
    "C-075": "tests/test_receipt_store.py",
    # C-076: "SQLite file, WAL, exports, backups, and diagnostic logs are local persistence boundaries"
    "C-076": "tests/test_receipt_store.py, tests/test_proof_receipts.py",
    # C-078: "Credentials are stored in $XDG_CONFIG_HOME/verdict/credentials.env"
    "C-078": "tests/test_credentials_store.py",
    # C-079: "Directory permissions: 0700, File permissions: 0600"
    "C-079": "tests/test_credentials_store.py",
    # C-080: "Atomic writes via temp file + os.replace()"
    "C-080": "tests/test_credentials_store.py",
    # C-081: "Permission enforcement: Refuses to load when group/world readable"
    "C-081": "tests/test_credentials_store.py",
    # C-082: "Never in YAML: Secrets are never written to verdict.yaml"
    "C-082": "tests/test_credentials_store.py",
    # C-083: "Never in argv: Set via getpass() or --stdin, never command arguments"
    "C-083": "tests/test_credentials_store.py",
    # C-084: "Exported environment variables always win over stored values"
    "C-084": "tests/test_credentials_store.py",
    # C-087: "All credential values are masked in output: set (len=N)"
    "C-087": "tests/test_credentials_masking.py",
    # C-088: "Full secrets never appear in logs, doctor output, or verdict.yaml"
    "C-088": "tests/test_doctor_presentation.py, tests/test_credentials_store.py",
    # C-089: "contracts/index.ts boundary is a strict TypeScript consumer of the v1 contracts"
    "C-089": "tests/test_contracts.py, tests/test_contract_parity.py",
    # C-090: "parseContract(name, value) is the only supported v1 parsing entry point"
    "C-090": "tests/test_contracts.py",
    # C-091: "Unknown fields and secret-like keys fail closed with a categorized ContractValidationError"
    "C-091": "tests/test_contracts.py",
    # C-092: "parseLegacyContract is an explicit migration entry point"
    "C-092": "tests/test_contracts.py",
    # C-093: "redactContractSecrets is for diagnostic/event output"
    "C-093": "tests/test_contracts.py",
    # C-095: "verdict detect --offline --json for a no-probe offline provider status report"
    "C-095": "tests/test_actions_cli_wiring.py",
    # C-097: "verdict route 'summarize this change' --criticality low --terse for operator-configured live route"
    "C-097": "tests/test_actions_cli_wiring.py",
    # C-098: "verdict simulate 'summarize this change' for a no-send forecast"
    "C-098": "tests/test_actions_cli_wiring.py",
    # C-099: "verdict orchestrate runs the full goal-to-receipt pipeline (ADR-036)"
    "C-099": "tests/test_orch_runtime.py, tests/test_orchestration_dispatch.py",
    # C-100: "verdict watch <run-id> for live TUI view"
    "C-100": "tests/test_orch_tui.py",
    # C-101: "verdict eligibility for eligibility ladder"
    "C-101": "tests/test_actions_cli_wiring.py",
    # C-102: "verdict run-receipt <run-id> for receipt verification"
    "C-102": "tests/test_actions_cli_wiring.py, tests/test_orch_receipt.py",
    # C-103: "verdict autodev-golden-path --objective --repo --json for offline proof"
    "C-103": "tests/test_autodev_wiring.py, tests/test_autodev_operational_loop.py",
    # C-104: "verdict failover-proof --memory-path --json for offline proof"
    "C-104": "tests/test_autodev_wiring.py, tests/test_failover_replay_proof.py",
    # C-105: "VERDICT_MEMORY_DB=/tmp/failover.db verdict replay <session-id> --json"
    "C-105": "tests/test_autodev_wiring.py, tests/test_failover_replay_proof.py",
    # C-106: "Maturity matrix with status for each capability" - documentation
    # Keep as is
    # C-108: "Proof Matrix: PM-001 through PM-021 with verified/observed/partial status" - verified
    # Has docs/proof/proof_matrix.v1.json - that's proof evidence
    "C-108": "docs/proof/proof_matrix.v1.json, scripts/generate_proof_matrix.py",
}

# Claims to relabel from "shipped" to "proposed" or "observed"
RELABEL_MAP = {
    # C-018: "No cost saving is claimed yet" - this is a negative claim about what's NOT claimed
    # C-019: "Live orchestration requires a gateway" - limitation
    # C-024: "Version 0.5.0, active development" - metadata
    # C-046: "Performance: No CI-backed latency SLA is currently claimed" - negative claim
    # C-049: "Example: local gateway" - just an example
    # C-071: "Verdict is a control plane for coding work" - description
    # C-072: "It is not an LLM gateway, and no cost saving is claimed" - negative claim
    # C-106: "Maturity matrix with status for each capability" - documentation
    # These are negative claims or documentation, not "shipped" features
    "C-018": "proposed",  # Actually it's a statement about what is NOT claimed
    "C-019": "proposed",  # Limitation
    "C-024": "proposed",  # Metadata
    "C-046": "proposed",  # Negative claim
    "C-049": "proposed",  # Example
    "C-071": "proposed",  # Description
    "C-072": "proposed",  # Negative claim
    "C-106": "proposed",  # Documentation
    # C-108 is already "verified" - good
}


def main():
    content = CLAIMS_PATH.read_text()
    lines = content.split("\n")
    new_lines = []

    for line in lines:
        if line.startswith("| C-") and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 6:
                claim_id = parts[1].strip()
                label = parts[4].strip()
                evidence_col = parts[5].strip()

                # Check if we need to relabel
                if claim_id in RELABEL_MAP:
                    parts[4] = f" {RELABEL_MAP[claim_id]} "
                    print(f"Relabelled {claim_id}: {label} -> {RELABEL_MAP[claim_id]}")

                # Check if we need to add test evidence
                if claim_id in TEST_EVIDENCE_MAP:
                    new_evidence = TEST_EVIDENCE_MAP[claim_id]
                    # Merge with existing evidence
                    existing_paths = [ep.strip() for ep in evidence_col.split(",") if ep.strip()]
                    new_paths = [ep.strip() for ep in new_evidence.split(",")]
                    # Combine, avoiding duplicates
                    all_paths = existing_paths + [p for p in new_paths if p not in existing_paths]
                    parts[5] = f" {', '.join(all_paths)} "
                    print(f"Updated evidence for {claim_id}: {evidence_col} -> {parts[5]}")

                line = "| " + " | ".join(parts[1:-1]) + " |"

        new_lines.append(line)

    CLAIMS_PATH.write_text("\n".join(new_lines))
    print("CLAIMS.md updated successfully")


if __name__ == "__main__":
    main()
