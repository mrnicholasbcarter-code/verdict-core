# OpenSpec change: anthropic messages relay

## Proposal

Add an Anthropic-compatible `POST /v1/messages` surface to the Verdict HTTP relay so Claude Code can run through the same admission → dispatch → receipt control plane as OpenAI clients, with protocol translation delegated to the upstream gateway (OmniRoute `/v1/messages` already translates Anthropic ⇄ provider formats). No second protocol stack lives in Core; Verdict owns only the boundary: routing decision, local-field stripping, context-pack injection, identity/proof enforcement, receipts, and stream terminal semantics.

## What Existing Docs Do Not Say

- `openspec/specs/` has no `relay-anthropic-messages` capability; the relay surface family is chat + responses only.
- `verdict/relay.py:171 protocol_for_surface` raises `ValueError` on any surface other than chat/responses.
- `verdict/stream_terminal.py:99` `SSETerminalParser.__init__` rejects surfaces outside `{chat, responses}`; Anthropic SSE terminal semantics (`message_stop` after `content_block_stop`) do not exist.
- `verdict/context_inject.py:inject_context_pack` handles `responses` and default-chat shapes only; an Anthropic payload has a top-level `system` field, not a leading system message.
- `verdict/harness_claude.py` never writes `ANTHROPIC_BASE_URL` and certifies Messages as unsupported.
- `docs/guides/harnesses.md` documents the Messages gap as a remaining limitation.

## Specification

See [specs/relay-anthropic-messages/spec.md](specs/relay-anthropic-messages/spec.md).

## Impact

- New spec delta `relay-anthropic-messages` with requirements mirroring the existing relay surfaces (admission parity, proof enforcement, receipts, named drops, stream terminal semantics, local-field stripping, context injection into the Anthropic `system` field).
- Affected code: `verdict/api.py` (new route + `_relay_completion` surface), `verdict/relay.py` (protocol id), `verdict/stream_terminal.py` (messages surface terminal semantics), `verdict/context_inject.py` (messages surface injection), `verdict/proxy.py` (messages forward path), `verdict/harness_claude.py` (native env upsert + truthful certification), docs (`docs/guides/harnesses.md`, `docs/guides/harness-bootstrap.md`, `docs/guides/coding-agent-gate.md`), and tests covering each requirement.
- Existing chat/responses behavior is unchanged: all existing relay, stream, and admission tests must pass untouched.
