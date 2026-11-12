# Tasks

## 1. Relay surface

- [ ] 1.1 `protocol_for_surface("messages")` returns `anthropic.messages`; unknown surfaces still raise: `tests/test_anthropic_messages_relay.py`
- [ ] 1.2 `POST /v1/messages` routes through `_relay_completion` with `surface="messages"`; admission denial, proof denial, and empty attempt set fail closed before any upstream byte, identical to chat: `tests/test_anthropic_messages_relay.py`
- [ ] 1.3 Upstream path is `/v1/messages`; client model replaced by admitted model; reserved local fields stripped; server credentials only: `tests/test_anthropic_messages_relay.py`
- [ ] 1.4 `x-api-key` accepted when it matches the server token; conflicting credentials rejected; header never forwarded: `tests/test_anthropic_messages_relay.py`
- [ ] 1.5 Messages-surface errors use the Anthropic error envelope; chat/responses envelopes unchanged: `tests/test_anthropic_messages_relay.py`

## 2. Context injection

- [ ] 2.1 Messages injection prepends the compiled pack to `system` (absent, string, block list) and never rewrites `messages`: `tests/test_anthropic_messages_relay.py`
- [ ] 2.2 Unsupported `system` shape skips injection with a recorded reason and no claimed digest: `tests/test_anthropic_messages_relay.py`

## 3. Stream terminal semantics

- [ ] 3.1 `message_stop` after content is checked success; EOF before it, `error` events, and data after `message_stop` fail closed; bytes pass through unchanged: `tests/test_stream_terminal.py`, `tests/test_anthropic_messages_relay.py`

## 4. Harness and docs

- [ ] 4.1 `verdict harness claude enable` sets `ANTHROPIC_BASE_URL` to the Verdict origin root, never `:20128`, never writes token values; disable restores exactly: `tests/test_harness_claude.py`
- [ ] 4.2 Certification notes state the Messages path is gateway-supported; docs (`docs/guides/harnesses.md`, `docs/guides/harness-bootstrap.md`, `docs/guides/coding-agent-gate.md`) point Claude Code at Verdict

## 5. Regression

- [ ] 5.1 Existing chat and responses relay, stream, and admission tests pass unchanged
