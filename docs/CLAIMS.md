# Public Claim Inventory

> Conservative claim registry. Shipped means behavior-specific test evidence was inspected;
> proposed means this audit has not established the full claim, not that the feature is absent.
> Observed rows cite retained proof, not fresh live validation. Source line numbers are historical.

| ID | Claim (short quote) | Source file:line | Label | Evidence |
|----|---------------------|------------------|-------|----------|
| C-001 | "A control plane for coding agents that shows its work" | README.md:11 | proposed | verdict/orchestration/run.py |
| C-002 | "Model admission, bounded recovery, a separate review route and receipts you can verify" | README.md:12 | proposed | verdict/eligibility.py, verdict/orchestration/recovery.py, verdict/orchestration/review.py, verdict/receipt_store.py |
| C-003 | "Checks models before trusting them" | README.md:44 | shipped | verdict/eligibility.py, tests/test_eligibility_gate.py |
| C-004 | "Uses eligible free and already-paid capacity first" | README.md:45 | proposed | verdict/intelligence.py, verdict/admission.py |
| C-005 | "Recovers on its own when a model fails" | README.md:46 | shipped | verdict/orchestration/recovery.py, tests/test_orch_recovery.py |
| C-006 | "Gets a separate review route" | README.md:47 | shipped | verdict/orchestration/runtime.py, tests/test_orch_runtime.py |
| C-007 | "Hands you a receipt you can verify" | README.md:48 | shipped | verdict/orchestration/receipt.py, tests/test_orch_receipt.py |
| C-008 | "Asks before risky actions" | README.md:49 | proposed | verdict/eligibility.py, verdict/commands/parsers_harness.py |
| C-009 | "verdict demo ships in verdict-core on PyPI (since 0.4.0)" | README.md:68 | proposed | verdict/flagship_demo.py |
| C-010 | "verdict eligibility --verified shows each listed model's evidence-backed status" | README.md:81 | proposed | verdict/commands/parsers_models.py, verdict/eligibility.py |
| C-011 | "/eligibility refresh shows bounded request plan grouped by capacity class" | README.md:88 | proposed | verdict/eligibility.py, verdict/commands/parsers_models.py |
| C-012 | "verdict harness prime select/restore previews change, asks confirm, backs up file" | README.md:97 | proposed | verdict/commands/parsers_harness.py, verdict/harness_prime.py |
| C-013 | "/bootstrap claude gives honest Claude Code compatibility report" | README.md:103 | proposed | verdict/commands/parsers_harness.py, verdict/harness_claude.py |
| C-014 | "Smart Tab-completion in the prompt" | README.md:106 | proposed | verdict/tui_completion.py |
| C-015 | "verdict quickstart --non-interactive --dry-run makes one deterministic routing decision" | README.md:112 | proposed | verdict/flagship_demo.py |
| C-016 | "The retained live run (dogfood-bod-225-live-2026-09-29) used subscription workers, not free workers" | README.md:51 | observed | docs/proof/dogfood-bod-225-live-2026-09-29/README.md, docs/proof/dogfood-bod-225-live-2026-09-29/receipt.json |
| C-017 | "Free capacity still needs fresh admission evidence" | README.md:52 | proposed | verdict/eligibility.py, verdict/admission.py |
| C-018 | "No cost saving is claimed yet" | README.md:206 |  proposed  | docs/benchmarks/README.md, README.md |
| C-019 | "Live orchestration requires a gateway" | README.md:209 |  proposed  | docs/CONFIGURATION.md, verdict/orchestration/runtime.py |
| C-020 | "Worker model availability is external" | README.md:210 | proposed | README.md, verdict/omniroute.py |
| C-021 | "Independent review requires ocr on PATH" | README.md:213 | proposed | verdict/orchestration/review.py |
| C-022 | "Receipt integrity covers event logs, not LLM or OCR output" | README.md:214 | proposed | verdict/receipt_verifier.py, docs/THREAT_MODEL_RECEIPTS.md |
| C-023 | "No static fallback chains; OpenTelemetry is optional" | README.md:216 | proposed | verdict/orchestration/recovery.py, verdict/tracing.py |
| C-024 | "Version 1.0.0, active development" | README.md:217 |  proposed  | pyproject.toml, README.md |
| C-025 | "pip install verdict-core" | README.md:226 | proposed | pyproject.toml, .github/workflows/fresh-install.yml |
| C-026 | "verdict detect --json" | README.md:237 | proposed | verdict/commands/parsers_models.py |
| C-027 | "verdict probe task-coding --base-url ... --allow-live-probe --json" | README.md:238 | proposed | verdict/commands/parsers_models.py |
| C-028 | "Nine verified Mermaid diagrams" | README.md:264 | proposed | diagrams/ |
| C-029 | "Credential-free quickstart fixture with explicit exclusions" | README.md:268 | proposed | verdict/flagship_demo.py |
| C-030 | "Fail-closed capability passports" | README.md:269 | proposed | verdict/capability_passports.py, docs/CAPABILITY_PASSPORTS.md |
| C-031 | "Adaptive/quality/cost claims: simulated only" | USER_JOURNEY.md:98 | proposed | verdict/benchmarking.py, docs/benchmarks/README.md |
| C-032 | "Hard eligibility runs before advisory ranking" | docs/architecture.md:26 | proposed | verdict/eligibility.py, verdict/intelligence.py |
| C-033 | "Gate composes eligibility + intelligence" | docs/architecture.md:35 | proposed | verdict/gate.py |
| C-034 | "Eligibility Gate: availability-aware hard filtering with explicit unknown handling" | docs/architecture.md:41 | proposed | verdict/eligibility.py |
| C-035 | "Intelligence Service: Advisory ranking only — cannot bypass hard gates" | docs/architecture.md:50 | proposed | verdict/intelligence.py |
| C-036 | "Availability Cache: Bounded SWR cache" | docs/architecture.md:55 | proposed | verdict/availability_cache.py |
| C-037 | "Serve path / execution path: execution-path authority required" | docs/architecture.md:62 | proposed | verdict/serve_path.py, verdict/api.py |
| C-038 | "Dispatcher binds authorized selected_route" | docs/architecture.md:67 | proposed | verdict/dispatcher.py |
| C-039 | "OmniRoute transport: Optional HTTP transport for catalog inventory, execute, and health evidence" | docs/architecture.md:71 | proposed | verdict/omniroute.py |
| C-040 | "Core metadata store: Independently fetched model caps from models.dev and LiteLLM" | docs/architecture.md:79 | proposed | verdict/metadata/ |
| C-041 | "Orchestration layer (ADR-036): shipped verdict.orchestration control plane" | docs/architecture.md:86 | proposed | verdict/orchestration/, docs/adr/ADR-036-goal-to-receipt-orchestration.md |
| C-042 | "Request routing: decision and execution are separate" | docs/architecture.md:98 | proposed | verdict/api.py, verdict/relay.py, verdict/gate.py |
| C-043 | "Explainability: /v1/route/explain returns freshness + eligibility explain records" | docs/architecture.md:123 | proposed | verdict/api.py, verdict/availability_cache.py |
| C-044 | "Contracts: Versioned, strict dataclass contracts for all public APIs" | docs/architecture.md:128 | proposed | verdict/contracts.py, schemas/contracts.v1.json |
| C-045 | "Security: API key redaction, PII masking, Private host blocking, Fail-closed" | docs/architecture.md:144 | proposed | verdict/security.py |
| C-046 | "Performance: No CI-backed latency SLA is currently claimed" | docs/architecture.md:149 |  proposed  | docs/architecture.md |
| C-047 | "Installed-runtime provenance and maintenance refresh lane" | docs/CONFIGURATION.md:8 | proposed | verdict/orchestration/verified_refresh.py, docs/CONFIGURATION.md |
| C-048 | "Optional platform-neutral guidance (experimental, disabled by default)" | docs/CONFIGURATION.md:3 | proposed | docs/CONFIGURATION.md |
| C-049 | "Example: local gateway" | docs/CONFIGURATION.md:16 |  proposed  | docs/CONFIGURATION.md |
| C-050 | "Restricted gateway connections snapshots" | docs/CONFIGURATION.md:23 | proposed | verdict/gateway_lifecycle.py, docs/CONFIGURATION.md |
| C-051 | "Capability passports are Verdict's fail-closed evidence boundary for model qualification" | docs/CAPABILITY_PASSPORTS.md:3 | proposed | verdict/capability_passports.py |
| C-052 | "Three-valued capability states: supported, unsupported, unknown" | docs/CAPABILITY_PASSPORTS.md:11 | proposed | verdict/capability_passports.py |
| C-053 | "Separate claimed and observed evidence" | docs/CAPABILITY_PASSPORTS.md:12 | proposed | verdict/capability_passports.py |
| C-054 | "Source, observation time, expiry, confidence, evidence digest, and limitations on every signal" | docs/CAPABILITY_PASSPORTS.md:13 | proposed | verdict/capability_passports.py |
| C-055 | "Canonical serialization and integrity digest" | docs/CAPABILITY_PASSPORTS.md:15 | shipped | verdict/capability_passports.py, tests/test_capability_passports.py |
| C-056 | "Hard-requirement evaluation that accepts only fresh observed support" | docs/CAPABILITY_PASSPORTS.md:16 | proposed | verdict/capability_passports.py |
| C-057 | "A catalog claim never satisfies a hard requirement" | docs/CAPABILITY_PASSPORTS.md:18 | proposed | verdict/capability_passports.py |
| C-058 | "Missing, expired, malformed, or claim-only evidence resolves to unknown" | docs/CAPABILITY_PASSPORTS.md:18 | proposed | verdict/capability_passports.py |
| C-059 | "Fresh failed observation remains authoritative over a positive catalog claim" | docs/CAPABILITY_PASSPORTS.md:19 | proposed | verdict/capability_passports.py |
| C-060 | "Passport expiry also makes every capability unknown" | docs/CAPABILITY_PASSPORTS.md:20 | proposed | verdict/capability_passports.py |
| C-061 | "verdict autodev packet execute --context-output-policy opts in for one run" | docs/BOD-333-AUTODEV-CONTEXT.md:3 | proposed | verdict/commands/parsers_autodev.py |
| C-062 | "The flag is off by default" | docs/BOD-333-AUTODEV-CONTEXT.md:4 | proposed | verdict/autodev_run.py |
| C-063 | "Python packet launch API accepts output_policy=ContextOutputPolicy(enabled=True)" | docs/BOD-333-AUTODEV-CONTEXT.md:5 | proposed | verdict/autodev_run.py |
| C-064 | "No shared configuration, HOME storage, provider calls, or tool interception is added" | docs/BOD-333-AUTODEV-CONTEXT.md:6 | proposed | verdict/autodev_run.py, verdict/harness_prime.py |
| C-065 | "The policy observes the raw post-compilation context string" | docs/BOD-333-AUTODEV-CONTEXT.md:16 | proposed | verdict/context_pack.py |
| C-066 | "Raw passthrough is the only supported transform" | docs/BOD-333-AUTODEV-CONTEXT.md:18 | proposed | verdict/autodev_run.py |
| C-067 | "Compiler budget, security exclusions, and sanitization remain unchanged" | docs/BOD-333-AUTODEV-CONTEXT.md:19 | proposed | verdict/context_pack.py |
| C-068 | "Existing context receipt gets raw_artifact_pointer, output_policy, and output_metrics" | docs/BOD-333-AUTODEV-CONTEXT.md:24 | proposed | verdict/receipt_store.py, verdict/autodev_run.py |
| C-069 | "Input/output bytes measure only raw policy passthrough" | docs/BOD-333-AUTODEV-CONTEXT.md:27 | proposed | verdict/autodev_run.py |
| C-070 | "OFF preserves pack bytes, digest, receipt shape, and legacy receipt key" | docs/BOD-333-AUTODEV-CONTEXT.md:33 | proposed | verdict/autodev_run.py |
| C-071 | "Verdict is a control plane for coding work (plan, admit, run, recover, review, receipt)" | docs/REPO-METADATA.md:28 |  proposed  | verdict/orchestration/run.py, README.md |
| C-072 | "It is not an LLM gateway, and no cost saving is claimed" | docs/REPO-METADATA.md:29 |  proposed  | docs/benchmarks/README.md, README.md |
| C-073 | "The receipt ledger is a local, privacy-safe audit boundary" | docs/THREAT_MODEL_RECEIPTS.md:3 | proposed | verdict/receipt_store.py, docs/THREAT_MODEL_RECEIPTS.md |
| C-074 | "Decision-time route metadata, lifecycle state, verification status, hashes, and scope identifiers are the canonical assets" | docs/THREAT_MODEL_RECEIPTS.md:6 | proposed | verdict/receipt_store.py |
| C-075 | "Raw prompts, tool arguments, outputs, credentials, and provider tokens are outside the default storage boundary" | docs/THREAT_MODEL_RECEIPTS.md:8 | proposed | verdict/receipt_store.py |
| C-076 | "SQLite file, WAL, exports, backups, and diagnostic logs are local persistence boundaries" | docs/THREAT_MODEL_RECEIPTS.md:10 | proposed | verdict/receipt_store.py, verdict/receipt_verifier.py |
| C-077 | "Independent rehearsal attestation: Certification can consume receipt bytes signed by GitHub Actions OIDC and Sigstore" | docs/THREAT_MODEL_RECEIPTS.md:52 | proposed | .github/workflows/certify-rehearsal.yml |
| C-078 | "Credentials are stored in $XDG_CONFIG_HOME/verdict/credentials.env" | docs/credentials.md:32 | proposed | verdict/credentials_store.py |
| C-079 | "Directory permissions: 0700, File permissions: 0600" | docs/credentials.md:36 | proposed | verdict/credentials_store.py |
| C-080 | "Atomic writes via temp file + os.replace()" | docs/credentials.md:38 | proposed | verdict/credentials_store.py |
| C-081 | "Permission enforcement: Refuses to load when group/world readable" | docs/credentials.md:39 | proposed | verdict/credentials_store.py |
| C-082 | "Never in YAML: Secrets are never written to verdict.yaml" | docs/credentials.md:40 | proposed | verdict/credentials_store.py |
| C-083 | "Never in argv: Set via getpass() or --stdin, never command arguments" | docs/credentials.md:41 | proposed | verdict/credentials_store.py |
| C-084 | "Exported environment variables always win over stored values" | docs/credentials.md:52 | proposed | verdict/credentials_store.py |
| C-085 | "All credentials used by Verdict must be registered in verdict/credentials_registry.py" | docs/credentials.md:56 | proposed | verdict/credentials_registry.py |
| C-086 | "The drift test ensures no credential is read without being registered" | docs/credentials.md:56 | proposed |  |
| C-087 | "All credential values are masked in output: set (len=N)" | docs/credentials.md:64 | proposed | verdict/credentials_store.py |
| C-088 | "Full secrets never appear in logs, doctor output, or verdict.yaml" | docs/credentials.md:65 | proposed | verdict/doctor_diagnostics.py, verdict/credentials_store.py |
| C-089 | "contracts/index.ts boundary is a strict TypeScript consumer of the v1 contracts" | docs/typescript-contract-parity.md:3 | proposed | contracts/index.ts, contracts/src/index.ts |
| C-090 | "parseContract(name, value) is the only supported v1 parsing entry point" | docs/typescript-contract-parity.md:10 | proposed | contracts/src/index.ts |
| C-091 | "Unknown fields and secret-like keys fail closed with a categorized ContractValidationError" | docs/typescript-contract-parity.md:11 | proposed | contracts/src/index.ts |
| C-092 | "parseLegacyContract is an explicit migration entry point" | docs/typescript-contract-parity.md:16 | proposed | contracts/src/index.ts |
| C-093 | "redactContractSecrets is for diagnostic/event output" | docs/typescript-contract-parity.md:19 | proposed | contracts/src/index.ts |
| C-094 | "The TypeScript validator must remain no more permissive than the Python runtime loader" | docs/typescript-contract-parity.md:23 | proposed | contracts/tests/ |
| C-095 | "verdict detect --offline --json for a no-probe offline provider status report" | docs/USER_JOURNEY.md:9 | proposed | verdict/commands/parsers_models.py |
| C-096 | "verdict quickstart --non-interactive --dry-run --json for credential-free route decision" | docs/USER_JOURNEY.md:14 | proposed | verdict/flagship_demo.py |
| C-097 | "verdict route 'summarize this change' --criticality low --terse for operator-configured live route" | docs/USER_JOURNEY.md:18 | proposed | verdict/commands/parsers_routing.py |
| C-098 | "verdict simulate 'summarize this change' for a no-send forecast" | docs/USER_JOURNEY.md:20 | proposed | verdict/commands/parsers_routing.py |
| C-099 | "verdict orchestrate runs the full goal-to-receipt pipeline (ADR-036)" | docs/USER_JOURNEY.md:23 | proposed | verdict/orchestration/run.py, docs/adr/ADR-036-goal-to-receipt-orchestration.md |
| C-100 | "verdict watch <run-id> for live TUI view" | docs/USER_JOURNEY.md:29 | proposed | verdict/commands/parsers_runtime.py |
| C-101 | "verdict eligibility for eligibility ladder" | docs/USER_JOURNEY.md:30 | proposed | verdict/commands/parsers_models.py |
| C-102 | "verdict run-receipt <run-id> for receipt verification" | docs/USER_JOURNEY.md:31 | proposed | verdict/commands/parsers_routing.py |
| C-103 | "verdict autodev-golden-path --objective --repo --json for offline proof" | docs/USER_JOURNEY.md:35 | proposed | verdict/commands/parsers_autodev.py |
| C-104 | "verdict failover-proof --memory-path --json for offline proof" | docs/USER_JOURNEY.md:36 | proposed | verdict/failover_replay_proof.py, verdict/commands/parsers_autodev.py |
| C-105 | "VERDICT_MEMORY_DB=/tmp/failover.db verdict replay <session-id> --json" | docs/USER_JOURNEY.md:37 | proposed | verdict/commands/parsers_autodev.py |
| C-106 | "Maturity matrix with status for each capability" | docs/USER_JOURNEY.md:44 |  proposed  | docs/USER_JOURNEY.md |
| C-107 | "Fail-closed LLM routing control plane: cheapest qualified model, named drop reasons, receipts" | pyproject.toml:13 | proposed | pyproject.toml |
| C-108 | "Proof Matrix: PM-001 through PM-021 with verified/observed/partial status" | docs/PROOF_MATRIX.md:1 | observed |  docs/proof/proof_matrix.v1.json, scripts/generate_proof_matrix.py  |
