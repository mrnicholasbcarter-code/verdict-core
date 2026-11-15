# Running Claude Code through Verdict

Claude Code's default traffic is Anthropic Messages (`POST /v1/messages`).
Verdict runs Claude Code through the same admit → dispatch → receipt control
plane as OpenAI clients: Verdict owns the governing boundary (admit gates,
protocol `anthropic.messages`, context-pack injection into the Anthropic
`system` field, receipt emission, and Anthropic-shaped error envelopes), while
protocol translation for `/v1/messages` is delegated to the upstream OmniRoute
gateway.

## 1. Point Claude Code at Verdict, not OmniRoute

**Set `ANTHROPIC_BASE_URL` to the Verdict origin root — never OmniRoute (`:20128`), never `api.anthropic.com`, and never `/v1/messages`.**

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN="..."   # read by Claude Code from the process env
```

`ANTHROPIC_BASE_URL` is an origin root with **no** `/v1` suffix — Claude Code
appends `/v1/messages` itself. Auth goes through `ANTHROPIC_AUTH_TOKEN` (or
`x-api-key` equal to Verdict's server token); token values are **never written**
into `~/.claude/settings.json`. Claude Code reads them from the process
environment.

The Architect-locked way to set this is `verdict harness claude enable`:

```bash
verdict serve --host 127.0.0.1 --port 8000   # Verdict first
verdict harness claude enable                # sets ANTHROPIC_BASE_URL → Verdict root
verdict harness claude status                # prints token-env presence only, never values
verdict harness claude certify
verdict harness claude disable               # restores settings byte-for-byte
```

`enable`:

1. Health-checks Verdict (`/health`, then `/v1/models`) unless `--force`.
2. Copies `~/.claude/settings.json` → `settings.json.verdict.bak` first.
3. Upserts `env.ANTHROPIC_BASE_URL` (Verdict origin root) and
   `env.OPENAI_BASE_URL` (`http://127.0.0.1:8000/v1`) — both at port `:8000`,
   never `:20128`.
4. Appends SessionStart `verdict hook claude-gate` when missing, so every
   Claude Code session starts fail-closed.

`disable` restores the pre-enable backup byte-for-byte.

**Do not point Claude Code at OmniRoute.** An OmniRoute-looking `:20128` URL is
refused by `verdict harness claude enable` unless `--force` is used, and Claude
Code's `/v1/messages` traffic must cross Verdict's admit gate first. OmniRoute
is upstream transport behind `verdict serve`, not the harness base URL.

## 2. Supported /v1/messages subset (and explicit non-goals)

Verdict serves `POST /v1/messages` through the same relay path as
`/v1/chat/completions` and `/v1/responses`: body limits, JSON validation,
reserved-field rejection, authoritative routing, admission narrowing, route-bound
proof enforcement, evidence lifecycle, and receipts. A denied decision, a
missing or stale passport/proof, or an empty attempt set **fails closed before
any upstream byte is sent**.

### Supported

| Surface item | Verdict handling |
|---|---|
| `POST /v1/messages` (non-streaming) | Admitted route replaces client model; forwarded to upstream `/v1/messages` |
| Streaming (`stream: true`) | Bytes pass through unchanged; `message_stop` after content is checked success |
| `system` as string / block list / absent | Context pack prepended into `system`; `messages` array never rewritten |
| Client tool definitions + tool-use result blocks | Forwarded to upstream |
| `x-api-key` matching the server token | Accepted as Claude Code's auth |
| Bearer auth equal to the server token | Accepted on all surfaces |

### Non-goals (fail closed, explicitly not supported)

- `GET /v1/messages/count_tokens` → **404**, documented as out of scope
- Anthropic beta endpoints → **404**
- Prompt caching, extended thinking translation, full feature parity beyond what
  Claude Code sends
- Exact model selection as a certified claim — the Messages path is
  gateway-supported; certification stays **partial** until a live Claude Code
  session passes
- A second full Anthropic protocol stack inside Core — translation is delegated
  to OmniRoute

## 3. Passport / confirm gating

Admission for `/v1/messages` requires a **fresh prove-at-rest passport** plus a
**budgeted confirm** on the exact `anthropic.messages` route identity — a
chat-only passport is insufficient (`is_messages_qualified` checks the
`anthropic.messages` capability confirmation).

The SessionStart hook `verdict hook claude-gate` runs `verdict catalog
--management`:

- exit **0** = catalog qualified, Claude Code session proceeds
- exit **2** = blocked (`catalog_fetch_timeout`, empty catalog, or
  `passed: false`) — Claude Code treats exit 2 as a block, no request is sent

Expired OAuth / cookie / unproven identities become **named drops** before
select (`no_passport`, `passport_stale`, `confirm_failed`,
`confirm_unavailable`, …). An empty intersection fails closed
(`no_eligible_target`) — never a silent frontier Opus fallback.

Prove it before you enable:

```bash
verdict hook claude-gate; echo $?   # 0 = admitted, 2 = blocked
```

## 4. Where receipts and named drops appear

Every `/v1/messages` admission emits a receipt recorded on the route-bound
evidence path:

- Decision log: `verdict-decisions.jsonl` (override via `LLMGATE_LOG_PATH`)
- Outcome receipts beside the decision log (`VERDICT_RECEIPTS_DB` or
  `verdict-decisions.jsonl` outcome file)
- `verdict run-receipt <run-id>` verifies the digest-bound receipt tied to the
  SHA-256 of `events.jsonl`

Receipt fields to assert on a messages decision:

| Field | Expect |
|---|---|
| `admitted` / `chosen` | Concrete admitted identity (not `anthropic/claude-opus-5`, not `auto/*`) |
| `exclusions[].reason` | Named drops: `inactive_unconnected`, `not_free_tier`, `metadata_ghost`, `opaque_auto`, `no_passport`, `passport_stale`, `confirm_failed`, … |
| `pack_state` | `empty` \| `partial` \| `hydrated` \| `failed` — savings blocked until `hydrated` |
| `pack_digest` / `pack_state` | `sha256:…` cheap-path pack; a digest alone is not hydrated |
| `passport` + `confirm` arrays | Fresh prove-at-rest passport + budgeted confirm on the chosen identity |
| `x-verdict-model` response header | Admitted model replacing the client-supplied model |

Named drops are not re-admitted: once dropped, a route stays dropped within the
budget window.

## 5. What fail-closed errors look like

Errors on `/v1/messages` use the **Anthropic error envelope** so Claude Code
renders them natively:

```json
{
  "type": "error",
  "error": {
    "type": "authentication_error",
    "message": "authentication failed"
  }
}
```

| Situation | Verdict response | Body shape |
|---|---|---|
| Missing/invalid credentials | `401` / `403` | Anthropic `{"type":"error","error":{...}}` |
| Denied admission / empty pool | `503` | Anthropic error envelope |
| Missing / stale passport / proof | `503`, no upstream byte | Anthropic error envelope naming the denial |
| Upstream execution failure | `502` | Anthropic error envelope |
| `count_tokens` / beta endpoint | `404` | Anthropic error envelope |
| Unsupported `system` shape | Injection skipped, request proceeds; reason recorded on receipt | — |

Unlike chat/responses (`{"error": {...}}`), messages-surface errors always wrap
in `{"type": "error", "error": {...}}`; headers carry `x-verdict-model`,
`x-verdict-decision` (`denied`), `x-verdict-transport-outcome`, and evidence IDs
(`x-verdict-evidence-id`, `x-verdict-request-id`, `x-verdict-correlation-id`).

Fail-closed means: on any denial, Verdict sends **nothing** upstream and Claude
Code surfaces the Anthropic error — never a silent fall-through to
`api.anthropic.com` or OmniRoute.

## 6. Reference

- Transport-reuse analysis (gateway delegation vs a second protocol stack in
  Core): [`openspec/changes/anthropic-messages-relay/design.md`](../../openspec/changes/anthropic-messages-relay/design.md)
- Claude Code harness CLI: `verdict harness claude {discover,enable,disable,status,certify}`
- Fail-closed serve setup: [coding-agent-gate.md](coding-agent-gate.md)
- Admit / prove / confirm smoke: [admit-prove-confirm-smoke.md](admit-prove-confirm-smoke.md)
- Capability passports: [CAPABILITY_PASSPORTS.md](../CAPABILITY_PASSPORTS.md)

## Existing docs alignment

No remaining docs direct Claude Code at OmniRoute or `:20128`. The harness
guides (`docs/guides/harnesses.md`, `docs/guides/harness-bootstrap.md`,
`docs/guides/coding-agent-gate.md`) already set `ANTHROPIC_BASE_URL` to the
Verdict origin root (`:8000`, no `/v1`) and state that an OmniRoute-looking
`:20128` URL is refused.
