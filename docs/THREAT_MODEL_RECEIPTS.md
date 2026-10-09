# Durable receipt threat model

The receipt ledger is a local, privacy-safe audit boundary. It stores redacted
metadata and hashes by default; it is not a prompt archive or a credential
vault. Operators remain responsible for host access controls, disk encryption,
SQLite backups, and upstream provider retention.

## Assets and trust boundaries

- Decision-time route metadata, lifecycle state, verification status, hashes,
  and scope identifiers are the canonical assets.
- Raw prompts, tool arguments, outputs, credentials, and provider tokens are
  outside the default storage boundary and require explicit field allowlists.
- The SQLite file, WAL, exports, backups, and diagnostic logs are local
  persistence boundaries. A replacement storage provider may be added without
  changing receipt semantics.

## Threats and controls

| Threat | Control | Residual risk |
| --- | --- | --- |
| Local attacker edits the database | Canonical payload hashes, metadata-bound record hashes, per-scope previous-hash chains, `verify_integrity`, and `doctor` | A host attacker who can replace the database and application can also replace verification code; use disk and deployment controls |
| Malicious retrieved content injects secrets into receipts | Recursive field redaction, credential/URL scrubbing, and raw-content fields redacted unless explicitly allowlisted | Heuristic scrubbing cannot classify every arbitrary sensitive string; typed envelope producers should provide safe metadata |
| Compromised adapters emit cross-scope records | Scope is required for durable API evidence reads/writes and SQL-filtered at boundaries | The generic compatibility API accepts a caller-provided scope string; deployments must bind it to authenticated tenancy |
| Log or export exfiltration | Exports are deterministic and scoped; default receipt fields are redacted; no provider credential vault is owned | Operators must protect exported files and may choose metadata-only exports |
| Duplicate or racing terminal callbacks | SQLite WAL, busy timeout, serialized `BEGIN IMMEDIATE`, event identity, and terminal first-write semantics | Conflicting attempts are rejected; a separate audit sink is needed if rejected attempts must be retained |
| Retention removes observed evidence | Append-only tombstones hide records from normal reads while preserving deletion reason and target hash linkage | Tombstones, WAL files, and backups can retain historical data until operator purge policy runs |

## Operational requirements

Use a durable `VERDICT_RECEIPTS_DB` path for authenticated API deployments and
back it up before migrations. Run `ReceiptStore.doctor()` after restore or
unexpected shutdown. Treat an invalid chain as an audit failure: replay and
durable explain lookups fail closed rather than claiming a valid result.

Raw-field allowlists should be narrow, exact paths controlled by the deployment
owner. They do not grant access to credentials or authorize external provider
calls. Replay only reads local receipt facts; it never contacts a model,
provider, adapter, or network endpoint.

## Independent rehearsal attestation

Certification can consume receipt bytes signed by GitHub Actions OIDC and
Sigstore. The trusted signer is the pinned
`mrnicholasbcarter-code/verdict-core/.github/workflows/certify-rehearsal.yml`
workflow, running on `refs/heads/main` at the exact certified source SHA.
The workflow uses the protected `certification` environment and GitHub-hosted
runners. The verifier enforces repository, signer workflow, source ref, source
and signer digests, issuer, and SLSA predicate through `gh attestation verify`.
It then checks the SHA256 subject in verified output against the actual receipt
bytes. Raw bundle predicate fields are never accepted as signer identity.

The verifier still rebuilds each receipt from its graph and event evidence.
Both clean and chaos must be `COMPLETE`. Clean must have no injected faults;
chaos must record a failed injected worker attempt. Producer SHA equality is
exact on this path, even if only documentation changed between commits.
A missing bundle or unavailable verifier is `INCOMPLETE`. A failed verification,
digest mismatch, stale producer, malformed output or semantic failure is `FAIL`.
Unattested local evidence and self-asserted evidence flags cannot certify.

| Threat | Control | Residual risk |
| --- | --- | --- |
| Local forger rewrites a receipt and recomputes its own digest | GitHub/Sigstore signed subject digest plus semantic replay | A signer or trusted workflow compromise can sign false evidence |
| Receipt from a different workflow, branch or commit is replayed | Pinned certificate signer workflow, main ref, exact source and signer SHA; exact producer SHA | Trusted main code and Actions administration remain security boundaries |
| Attestation is missing or verifier cannot start | Non-certifying `INCOMPLETE`; timeout or invalid verifier result fails | Availability does not imply certification |
| Gateway lies about route, identity or response | Model identity remains explicitly receipt-reported | Gateway and providers are operator-controlled; OIDC does not attest provider execution |
| Workflow or provider output leaks credentials | Environment protection, masked gateway secrets, no provider output in logs, known-secret scan before upload | Arbitrary sensitive content needs operator review; masking is not a universal secret scanner |

This proves that a specific GitHub workflow on main at a specific SHA produced
specific receipt bytes. It does not move generation of rehearsal content outside
Verdict. It does not prove provider honesty, model execution, correctness of
accepted work, or reviewer competence. Protect main, workflow changes, environment
approvals, gateway configuration and scoped credentials. A compromised certifier
host can replace verification code and is outside this local verification boundary.

See [certification operations](certification/README.md#attested-ci-rehearsals).
Adding this lane does not create secrets or enable `VERDICT_REQUIRE_CERTIFIED_BUNDLE`.
