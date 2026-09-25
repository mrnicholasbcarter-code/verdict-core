# Threat Model

**Scope:** `verdict-core` alpha proxy and decision engine — local developer tool and
operator-hosted inference router. Not a certified production security product.

**Method:** STRIDE (Spoofing, Tampering, Repudiation, Information disclosure,
Denial of service, Elevation of privilege).

**Last updated:** 2025 — read `SECURITY.md` for the current security policy and
vulnerability reporting procedure.

---

## System overview

`verdict` is a local Python process. It exposes an HTTP API (FastAPI), routes
LLM inference requests to a configured upstream provider or OmniRoute gateway,
writes redacted routing receipts to a local SQLite ledger (ADR-017), and
persists decision logs to a JSONL file. There is no hosted service, multi-tenant
database, or user-authentication store.

### Key components

| Component | File |
|-----------|------|
| Auth and SSRF helpers | `verdict/security.py` |
| HTTP API and middleware | `verdict/api.py` |
| Receipt persistence and redaction | `verdict/receipt_store.py` |
| Routing receipt builder | `verdict/routing_receipt.py` |
| Decision logger | `verdict/logger.py` |
| Upstream transport | `verdict/proxy.py` |

### Trust boundaries

1. **Caller → proxy API** — bearer token (`LLMGATE_AUTH_TOKEN`) or loopback-only
   anonymous mode (`LLMGATE_ALLOW_ANONYMOUS=true` + `--host 127.0.0.1`).
2. **Proxy → upstream provider** — operator-configured URL
   (`LLMGATE_UPSTREAM_BASE_URL`); SSRF guards enforce no private/loopback
   addresses unless explicitly listed in `LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS`.
3. **Receipt ledger** — local SQLite file owned by the host OS user; no network
   access.
4. **Decision log** — append-only JSONL file on the local filesystem.

---

## S — Spoofing

### S-1: Caller identity spoofing

**Threat:** An attacker sends API requests without a valid token to obtain
routing decisions or trigger upstream calls.

**Controls:**
- Every non-health route requires a `Bearer` token validated with
  `hmac.compare_digest` (`verdict/security.py` → `bearer_matches`).
- `verdict/api.py` middleware `caller_authentication` (line ~625) returns 401
  with `WWW-Authenticate: Bearer` when no token is supplied, and 403 on mismatch.
- Production startup fails if `LLMGATE_AUTH_TOKEN` is absent and anonymous mode
  is not explicitly enabled (`verdict/security.py` → `validate_server_security`).
- **Test:** `tests/test_security.py::test_proxy_requires_bearer_token_by_default`,
  `tests/test_security.py::test_proxy_rejects_invalid_bearer_without_leaking_token`.

**Residual risk:** Token strength depends on the operator; short or guessable
tokens are not rejected. No rate-limiting or account lockout is implemented.

### S-2: Anonymous mode on non-loopback

**Threat:** An operator misconfigures anonymous mode on a public interface,
exposing the proxy without authentication.

**Controls:**
- `validate_server_security` in `verdict/security.py` raises `ValueError` when
  `LLMGATE_ALLOW_ANONYMOUS=true` is combined with any non-loopback bind address.
- Hostnames `localhost` / `ip6-localhost` and the IPv4/IPv6 loopback range are
  the only accepted values.
- **Test:** `tests/test_security.py` (anonymous-mode tests).

**Residual risk:** DNS rebinding is not mitigated; operators should bind strictly
to `127.0.0.1`.

---

## T — Tampering

### T-1: Receipt ledger tampering

**Threat:** A local attacker modifies the SQLite ledger to alter routing audit
records.

**Controls:**
- Each receipt record carries a canonical payload hash and a metadata-bound
  record hash (`verdict/receipt_store.py` → `ReceiptRecord.record_hash`).
- A per-scope previous-hash chain allows `ReceiptStore.verify_integrity` and
  `ReceiptStore.doctor` to detect any gap or replacement
  (`verdict/receipt_store.py`).
- ADR-017 (`docs/adr/ADR-017-durable-privacy-safe-receipt-ledger.md`):
  "Reads, exports, replay, and durable explain lookups verify the scoped chain
  and fail closed on tampering."
- **Test:** `tests/security/test_launch_gate_evidence.py` (ledger integrity).

**Residual risk:** An attacker who controls the host and can replace both the
database and the application binary can also replace the verification code.
Disk encryption, OS access controls, and deployment integrity checks remain
the operator's responsibility.

### T-2: Upstream URL injection

**Threat:** A caller-supplied URL is used as the upstream target, enabling
request redirection to an attacker-controlled endpoint.

**Controls:**
- `verdict/api.py` builds the upstream transport once at startup from
  `LLMGATE_UPSTREAM_BASE_URL`; the caller cannot supply an upstream URL at
  request time.
- `validate_upstream_url` in `verdict/security.py` enforces `http`/`https`
  scheme, no userinfo, no query or fragment.
- `pin_upstream_url` resolves the hostname once before transport and rejects
  any address that is private, loopback, or link-local unless
  `LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS` explicitly lists it
  (`verdict/security.py` → `host_is_allowed`).
- **Test:** `tests/test_security.py` (SSRF / upstream URL tests).

**Residual risk:** The private-host allowlist is operator-managed; a
misconfigured allowlist can permit access to internal services.

---

## R — Repudiation

### R-1: Unacknowledged routing decisions

**Threat:** A caller denies having issued a request; no audit trail exists.

**Controls:**
- Every routing decision is appended to a JSONL decision log via
  `verdict/logger.py` → `log_decision`. The log records model, provider,
  reason, tier, and a sha256 fingerprint of the task (not the raw task text).
- Durable receipts in `verdict/receipt_store.py` provide an append-only ledger
  with sequence numbers and hash chains that cannot be silently rewound.
- ADR-017: "Process restarts and concurrent workers share one durable evidence
  authority."

**Residual risk:** The log and ledger are local files. Without off-host
shipping, an attacker who can delete local files can remove evidence. Operators
must configure log rotation, backups, and off-host archival independently.

### R-2: Duplicate or racing terminal callbacks

**Threat:** A race between two terminal events produces conflicting records, or
a second event silently overwrites the first.

**Controls:**
- SQLite WAL mode, busy timeout, and `BEGIN IMMEDIATE` serialisation in
  `verdict/receipt_store.py`.
- Terminal first-write semantics: a second terminal event for the same record
  is rejected rather than replacing the accepted outcome
  (`docs/THREAT_MODEL_RECEIPTS.md`).

**Residual risk:** Rejected duplicate terminal events are not retained in a
separate audit sink by default; a separate sink is needed if rejected attempts
must be auditable.

---

## I — Information Disclosure

### I-1: Credentials or prompts in receipts

**Threat:** Raw API keys, bearer tokens, or user prompts are written to the
receipt ledger or decision log and can be read by anyone with file access.

**Controls:**
- `redact_sensitive_dict` in `verdict/receipt_store.py` recursively replaces
  every field whose key matches `_SENSITIVE_KEY_PARTS` (api_key, apikey,
  secret, password, token, credential, authorization, auth, …) or
  `_RAW_KEY_PARTS` (prompt, messages, tool_arg, arguments, completion, output,
  transcript, request_body, response_body) with `"[REDACTED]"`.
- The allowlist mechanism requires exact field-level opt-in; the default is
  closed.
- `redact_text` in `verdict/security.py` strips `Authorization: Bearer …`
  headers and credential-pattern strings from diagnostic text.
- `routing_receipt_allowlist` in `verdict/routing_receipt.py` defines which
  fields survive to a routing receipt (measured token/cost evidence only, not
  raw content).
- Decision logs record only a sha256 fingerprint of the task
  (`verdict/security.py` → `fingerprint_text`), not the full text.
- ADR-017 and `docs/patterns/privacy-safe-execution-evidence.md`: "Redaction
  precedes persistence."
- **Test:** `tests/test_security.py::test_decision_logging_never_writes_full_prompt`.

**Residual risk:** Heuristic key-name matching cannot classify every
arbitrarily named sensitive field. Operators who add non-standard field names
must audit their allowlists. Encrypted-at-rest storage is the operator's
responsibility.

### I-2: Credentials in upstream URL or error messages

**Threat:** An API key embedded in a URL or returned in an error message is
logged or returned to the caller.

**Controls:**
- `validate_upstream_url` in `verdict/security.py` rejects URLs with userinfo
  (credentials in the URL).
- `redact_text` in `verdict/security.py` strips URL-embedded credentials and
  bearer tokens from any string before it is logged.
- SECURITY.md: "Never put an API key in a URL; use `LLMGATE_UPSTREAM_API_KEY`."

**Residual risk:** Providers may return secrets in response bodies; those are
not scrubbed by verdict before being forwarded to the caller.

### I-3: Cross-scope record leakage

**Threat:** A receipt lookup in one tenant scope returns records belonging to
another scope.

**Controls:**
- `verdict/receipt_store.py` SQL queries filter on `scope` at the DB layer.
- ADR-017: "Authenticated API startup fails closed when `VERDICT_RECEIPTS_DB`
  path is absent"; scope is required for durable API evidence reads/writes.
- `docs/THREAT_MODEL_RECEIPTS.md`: "Scope mismatches are indistinguishable from
  missing records."

**Residual risk:** The generic compatibility API accepts a caller-provided scope
string; deployments must bind it to authenticated tenancy. The alpha release
does not provide a multi-tenant authentication layer.

---

## D — Denial of Service

### D-1: Oversized request bodies

**Threat:** A caller sends an extremely large request body to exhaust server
memory or slow the process.

**Controls:**
- `verdict/api.py` enforces `LLMGATE_MAX_REQUEST_BYTES` (default 2 MiB) before
  parsing the JSON body; any excess returns HTTP 413.
- The limit is configurable by operators (line ~1367 in `verdict/api.py`).
- **Test:** `tests/test_security.py` (request size tests).

**Residual risk:** No rate-limiting or per-client connection quota is
implemented. A high-concurrency flood would require a reverse-proxy or OS-level
mitigation.

### D-2: Slow or hung upstream responses

**Threat:** A slow upstream holds open a connection indefinitely, exhausting
worker slots.

**Controls:**
- `LLMGATE_UPSTREAM_TIMEOUT_MS` configures a per-request timeout (default
  30 000 ms) in `verdict/api.py` → `_build_proxy`.
- The streaming wrapper in `verdict/api.py` enforces a per-stream byte cap
  (16 MiB by default) to prevent unbounded streaming reads.

**Residual risk:** No circuit-breaker or per-IP concurrency cap is implemented.

### D-3: Receipt ledger growth

**Threat:** Unbounded growth of the SQLite ledger exhausts disk space.

**Controls:**
- Tombstone-based logical deletion in `verdict/receipt_store.py` allows
  records to be hidden without rewriting history.
- ADR-017 notes WAL, backup, and disk-retention cleanup remain the operator's
  responsibility.

**Residual risk:** No automatic purge policy is implemented; operators must
configure retention and deletion schedules.

---

## E — Elevation of Privilege

### E-1: Anonymous mode on a non-loopback address

**Threat:** An operator accidentally starts the server in anonymous mode on
`0.0.0.0`, granting any network peer unauthenticated access.

**Controls:**
- `validate_server_security` in `verdict/security.py` raises at startup for
  any non-loopback host when anonymous mode is enabled — the server does not
  start.
- SECURITY.md documents this constraint explicitly.
- **Test:** `tests/test_security.py` (anonymous-on-non-loopback tests).

**Residual risk:** None known beyond DNS rebinding (see S-2).

### E-2: Upstream SSRF to internal services

**Threat:** An attacker causes the proxy to issue HTTP requests to internal
services (cloud metadata endpoints, internal APIs) by exploiting operator
misconfiguration.

**Controls:**
- `validate_upstream_url` in `verdict/security.py` rejects literal
  private/loopback/link-local IP addresses at configuration time.
- `host_is_allowed` / `pin_upstream_url` in `verdict/security.py` re-resolves
  the hostname immediately before transport and fails closed if the resolved
  address is non-global or multicast.
- DNS-rebinding between configuration time and transport time is mitigated by
  pinning the resolved address directly for the request.
- **Test:** `tests/test_security.py` (SSRF guard tests).

**Residual risk:** The `LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS` allowlist is
operator-managed; a broad allowlist can enable SSRF to listed private ranges.

### E-3: Scope privilege escalation via caller-supplied scope

**Threat:** A caller passes a different tenant's scope string to read or write
their receipts.

**Controls:**
- ADR-017 and `docs/THREAT_MODEL_RECEIPTS.md`: the generic compatibility API
  accepts a caller-provided scope string; authenticated API deployments must
  bind it to authenticated tenancy.

**Residual risk:** The alpha release does not implement a multi-tenant
authentication layer. Operators running multi-tenant deployments must add their
own scope-binding middleware.

---

## Residual risks summary

| Risk | Likelihood | Impact | Operator mitigation |
|------|------------|--------|---------------------|
| Host attacker replaces ledger + verification code | Low (host compromise required) | High | Disk encryption, OS ACLs, deployment integrity |
| Short or guessable bearer token | Medium | High | Use a cryptographically random token of ≥ 32 bytes |
| Private-host allowlist misconfiguration enables SSRF | Low | High | Keep allowlist empty unless intentionally routing to a local backend |
| Unbounded log/ledger growth | Medium | Medium | Configure retention, rotation, and off-host archival |
| No rate limiting / connection quotas | Medium | Medium | Front with a reverse proxy (nginx, Caddy) |
| Arbitrary sensitive field names bypass redaction | Low | Medium | Audit receipt allowlists; use standard field names |
| DNS rebinding | Low | Medium | Bind to `127.0.0.1`; use a loopback-only reverse proxy |
| Multi-tenant scope escalation | N/A (alpha) | High | Add scope-binding middleware before multi-tenant deployment |

---

## Out of scope

- Provider-side data retention and compliance (each provider's own terms apply).
- Encryption-at-rest for the SQLite ledger or JSONL log.
- Multi-tenant authentication.
- Prompt quality, model output accuracy, and hallucination risks.
- Vulnerabilities requiring a compromised developer machine.

See `SECURITY.md` for the vulnerability reporting procedure.
