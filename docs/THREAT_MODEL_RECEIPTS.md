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
