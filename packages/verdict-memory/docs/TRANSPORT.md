# P actual transport contract (2026-10-16, read-only discovery)

Sources: authenticated `tools/list` on existing `127.0.0.1:49374/mcp` (saved schema: /tmp/bod80-p-live-schemas.json); ai-memory source reference `work/refs/ai-memory/crates/ai-memory-mcp/src/server.rs`: write 4348–4619, read 4657–4846, delete 5044–5126, query 2687–3194. Codebase-memory ai-memory-prime search_code and Serena package symbols used. Prior authority: IMPLEMENTATION-BOUNDARIES.md and P-REPLACEMENT-TRANSPORT-REPAIR.md; session_memory parent turn1661. No backend writes performed.

- JSON-RPC 2.0 requests with matching ids; `tools/call` result is `{content:[{type:"text",text:"<JSON object>"}]}`. Errors may be JSON-RPC errors or `isError` tool results. HTTP Accept includes application/json,text/event-stream. Server is stateless HTTP.
- `memory_write_page`: required path/body; explicit workspace/project together. Optional kind supported (<=64). Arbitrary frontmatter, event_digest, metadata NOT supported. Whole-page replacement, NOT CAS. Result `{page_id,path,checkpoint}`; does NOT acknowledge a digest.
- `memory_read_page`: explicit path/workspace/project. Result `{path,title,body,frontmatter}`; optional served_from. It returns no project/workspace names: bound explicit request scope plus validated embedded identity establishes project scope. Missing page is RPC -32603 message `page <path> not found in resolved scope <label>` (server.rs4830). Other errors must not be treated as absence.
- `memory_query`: query,workspace,project,limit,answer:false. Result has hits (may also contain raw_hits/global_scope_hits). Non-global hits may omit project/workspace. Ignore global/raw results. Each candidate must be re-read by exact package path in configured scope; validate canonical embedded lifecycle object before use. Optional narrower query.scope must match the stored event.scope, never relabel it.
- `memory_delete_page`: path,workspace,project. Result `{path,deleted:true,pre_checkpoint,checkpoint}`. Exact path and boolean result required, followed by scoped read confirming absence. Pre-read validates package envelope and optional ref digest.

Representation: body is a fixed Markdown H1 plus canonical JSON object containing versioned lifecycle envelope, sanitized event, event_digest and content_fingerprint. All fit supported body. No unsupported frontmatter argument. Validate hashes, computed source idempotency key and exact normalized `verdict-memory/events/<64hex>.md` on every read. Fixed directory (not kind) prevents changed-kind source conflicts from escaping to a second path.

Put performs scoped read before write. Existing same source key + same sanitized content fingerprint is replay (no write; original source timestamp retained). Changed logical content => typed conflict (no overwrite). Absent -> write -> exact ACK path -> scoped read with exact envelope comparison before durable ACK. Cross-process race remains: service has no CAS; this contract requires a single writer per source namespace. No claim of server CAS/exactly-once. Read-after-write proves observed durability, not future immutability. Outbox is operational replay only.

Security: deterministic policy before persistence; sanitize nested fields once consistently before outbox and remote representation; credentials never printed. Remote HTTPS requires verification, no redirects; existing loopback HTTP allowed. Experimental 0.1.0a1; Rust parity unmeasured. Live writes remain HOLD pending independent parent review.

## Enforcement and limits

### Outbound secret guard (BOD-423)

Every JSON-RPC body `AiMemoryClient._mcp` sends first passes
`contracts.guard_outbound`:
- every string, keys included, has bare API tokens replaced with `[REDACTED]`:
  `sk-<alnum>` and segmented `sk-proj-`/`sk-ant-`/`sk-or-v1-`/`sk-svcacct-` forms that
  contain a digit, plus `xai-`, `gsk_`, `hf_`, `glpat-` and Stripe `sk_`/`rk_`
  `test_`/`live_` keys. Each must start a word, so `task-`, `risk-`, `desk-` and
  `disk-` text is kept (findings: `/workspace/hive/findings/ai-memory-sk-scanner-gap.md`
  on the operator's box);
- the result is re-serialized and scanned again;
- if redaction raises, a value cannot be checked (not JSON), or a token survives, the
  call raises `MemoryProviderError(INVALID_REQUEST, "... request not sent")`, records
  `last_error`, and **sends nothing**. `search` turns that into a typed non-available
  `RecallResult`, never an empty success.

`redact_secrets` and `MemoryCapturePolicy` use the same pattern
(`BARE_API_TOKEN_RE`), so event content holding a bare token is denied
(`secret_detected`) and metadata is redacted in the outbox and remote page.
The `Authorization` header is not a body and is not touched. This is a client-side
guard only; the ai-memory server-side scanner gap is tracked separately.

All put/delete operations take a nonblocking POSIX advisory lock keyed by endpoint/workspace/project under `~/.cache/verdict-memory/locks`. Contention returns typed DEGRADED and the outbox retries. Cooperating processes on this host must use the same lock directory; deployments must assign this package as sole writer for its namespace. Cross-host writers or tools that ignore that lock are unsupported. ai-memory has no conditional write API, so the client cannot guarantee cross-host CAS. The lock is operational coordination, not a new storage authority.

`ProviderErrorCode.CONFLICT` is a permanent replay failure and dead-letters in the mirror. Ref revision is the persisted sanitized event digest (not backend revision). Sanitization runs identically before outbox and direct put; fingerprints bind the sanitized logical object. Same-source replay preserves the first persisted created_at. No-source replay retains digest identity semantics.

Existing old-format pages are deliberately not migrated or trusted: only validated `verdict-memory/events/<64hex>.md` body envelopes are returned/deleted. No fallback to raw snippets. Package memory is advisory; exact envelope equality does not confer authority.

Read-only absence verification on 2026-10-16: existing bod80-memory-test unknown page returns RPC -32603 `page <exact-path> not found in resolved scope default/bod80-memory-test`; nonexistent isolated project returns -32602 `project '<configured-project>' not found in workspace '<configured-workspace>'`. Only these exact scoped absence signals are accepted before first write; other scope errors do not authorize a write. `memory_write_page` creates missing projects (actual tools/list schema). No project was created for this read-only check.
