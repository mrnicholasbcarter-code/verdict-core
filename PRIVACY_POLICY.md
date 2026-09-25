# Privacy Policy

**Scope:** `verdict-core` alpha proxy and decision engine.
**Effective date:** 2025.
**Contact:** See `SECURITY.md` for contact and vulnerability reporting details.

This document describes what data `verdict` processes, what is logged, how long
records are retained, how erasure works, and the relationship with upstream
providers. It applies to self-hosted deployments. Provider-hosted SaaS is not
currently offered.

---

## 1. Data processed

`verdict` is a local process. It processes the following categories of data:

| Category | Description | Stored in receipts/logs? |
|----------|-------------|--------------------------|
| Routing metadata | Model name, provider name, tier, policy fields, timing, cost estimate | Yes — unredacted |
| Task fingerprint | sha256 digest of the task text (not the text itself) | Yes — digest only |
| Candidate explanations | Redacted routing explanations, token/cost estimates | Yes — redacted |
| Request bodies | Full JSON request bodies from the caller | No — not persisted |
| Prompts / messages | Raw user prompts and conversation messages | No — redacted before persistence |
| Completions / outputs | Model response text | No — redacted before persistence |
| Tool arguments | Tool call arguments passed to a model | No — redacted before persistence |
| Credentials | API keys, bearer tokens, passwords | No — redacted before persistence and rejected in URLs |
| Caller IP / identity | HTTP peer address | No — not logged by default |
| Provider tokens | Upstream provider API keys | No — read from environment variables; never logged |

`verdict` does not collect, transmit, or store personal information by design.
The default receipt and log formats contain no prompts, completions, credentials,
or identifiable user content.

---

## 2. What is logged

### 2.1 Decision log (JSONL)

`verdict/logger.py` → `log_decision` writes one JSONL record per routing
decision. Each record contains:

- Model name, provider name, tier, reason, fallback flag.
- A sha256 fingerprint of the task (`verdict/security.py` → `fingerprint_text`).
- Standard operational fields: timestamp, status, safe error category.

**What is never written:** full task text, prompt messages, completions, API keys,
bearer tokens, or any credential string.

**Test:** `tests/test_security.py::test_decision_logging_never_writes_full_prompt`
asserts that the raw task text does not appear in any decision log entry.

### 2.2 Receipt ledger (SQLite)

`verdict/receipt_store.py` persists routing receipts in a local SQLite database
(path configured by `VERDICT_RECEIPTS_DB`). The `redact_sensitive_dict` function
applies recursive redaction before every write:

- Fields matching `_SENSITIVE_KEY_PARTS` (api_key, apikey, secret, password,
  token, credential, authorization, auth, account_id, private_key, access_key,
  client_secret) are replaced with `"[REDACTED]"`.
- Fields matching `_RAW_KEY_PARTS` (prompt, messages, tool_arg, arguments,
  completion, output, transcript, request_body, response_body) are replaced
  with `"[REDACTED]"`.
- String values are scrubbed for `Authorization: Bearer …` patterns and
  credential-style key=value strings.
- The allowlist mechanism (`routing_receipt_allowlist` in
  `verdict/routing_receipt.py`) permits only measured token/cost evidence fields
  to survive; raw content fields are excluded by default.

**Reference:** `docs/adr/ADR-017-durable-privacy-safe-receipt-ledger.md` and
`docs/patterns/privacy-safe-execution-evidence.md`: "Redaction precedes
persistence."

**Reference:** `docs/THREAT_MODEL_RECEIPTS.md`: "Raw prompts, tool arguments,
outputs, credentials, and provider tokens are outside the default storage
boundary."

### 2.3 What is not logged

- Full request or response bodies.
- Caller HTTP headers (including Authorization headers from the caller).
- Provider API keys or upstream credentials.
- IP addresses or session identifiers.

---

## 3. Retention and erasure

### 3.1 Retention window

The default GDPR-equivalent retention window is **30 days** for operational
receipts and memory records.

Reference: `docs/privacy/retention-erasure.md`.

Operators are responsible for configuring filesystem access policy, log
rotation, backup schedules, and deletion schedules appropriate to their data
and jurisdiction. `verdict` does not provide a hosted retention service,
automatic deletion schedule, encryption-at-rest guarantee, or compliance
certification.

### 3.2 Erasure procedure

1. Identify the requester's authorized storage scope.
2. Locate records in that scope using the durable record identifier or key.
3. Append a privacy-safe tombstone for each matching record. Tombstones contain
   only the target identifier and operation metadata; they do not copy deleted
   content.
4. Confirm that normal retrieval, search, export, and replay paths no longer
   return the tombstoned content.
5. Preserve only the minimum audit metadata needed to prove that erasure was
   performed.

Erasure is scope-bound. A request cannot read or remove records belonging to a
different scope. If the target is absent, the operation is idempotent and does
not create a content-bearing record.

An erasure request is honored without undue delay, and in any case within 30
days of the request.

**Test:** `tests/privacy/test_retention_erasure.py` exercises the 30-day
deadline against synthetic data and verifies that tombstoned records are
unreachable from ordinary retrieval.

Reference: `docs/privacy/retention-erasure.md`.

---

## 4. Telemetry

`verdict` does not currently ship a general-purpose telemetry emitter or a
telemetry-consent CLI switch. Routing evidence and orchestration receipts are
local execution records, not remote telemetry.

Any future telemetry sink must be opt-in, limited to operational aggregate
signals (event type, bounded correlation identifiers, timing, status), and must
not include prompt text, model output, tool arguments, credentials, or any
private content.

Reference: `docs/privacy/telemetry-consent.md`.

**Current status:** no telemetry records are written or transmitted by default.

---

## 5. Third parties and upstream providers

`verdict` routes inference requests to operator-configured upstream providers
(e.g. Anthropic, OpenAI, or OmniRoute). When a request is forwarded:

- The upstream provider receives the full request body as supplied by the caller.
- Provider-side data handling, retention, and privacy policies are governed by
  that provider's own terms of service.
- `verdict` does not add tracking identifiers or additional metadata to
  forwarded requests.
- Provider API keys are read from environment variables
  (`LLMGATE_UPSTREAM_API_KEY`, `OMNIROUTE_API_KEY`, or provider-specific
  variables) and are never written to logs or receipts.
- Upstream URLs are validated to exclude credentials in the URL
  (`verdict/security.py` → `validate_upstream_url`).

Operators must configure provider-side retention and privacy controls
separately.

---

## 6. Data security

- Bearer authentication (`LLMGATE_AUTH_TOKEN`) or loopback-only anonymous mode
  is required for every API endpoint (`verdict/security.py` →
  `validate_server_security`; `verdict/api.py` → `caller_authentication`).
- SSRF guards prevent routing to private/loopback/link-local addresses
  (`verdict/security.py` → `validate_upstream_url`, `host_is_allowed`,
  `pin_upstream_url`).
- Request bodies are bounded by `LLMGATE_MAX_REQUEST_BYTES` (default 2 MiB)
  to limit memory exposure (`verdict/api.py`).
- The receipt ledger uses append-only writes with hash chains to detect
  tampering (`verdict/receipt_store.py`; ADR-017).

---

## 7. Operator responsibilities

`verdict` is an alpha tool. Operators remain responsible for:

- Choosing a restrictive `log_path` and `VERDICT_RECEIPTS_DB` path.
- Filesystem access policies and disk encryption.
- Log rotation, backup, and deletion schedules.
- Provider-side privacy and retention configuration.
- Scrubbing existing logs and receipts before sharing bug reports or benchmark
  artifacts.
- Keeping `log_full_task` disabled unless the task text is known to be safe for
  the chosen retention period (see `SECURITY.md`).

---

## 8. Changes to this policy

This document is maintained in the `verdict-core` repository. Material changes
will be noted in `CHANGELOG.md`. This policy does not constitute a legal
privacy notice; operators who deploy `verdict` in a regulated context must
conduct their own assessment.

---

## 9. Contact

Report privacy concerns using the same channel as security vulnerabilities.
See `SECURITY.md` for contact details and the vulnerability reporting procedure.
