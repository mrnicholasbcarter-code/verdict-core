# Claude Code harness: point Claude Code at Verdict

Architect lock: `verdict harness claude discover|enable|disable|status|certify`.

These commands switch Claude Code onto Verdict for the **OpenAI-compatible**
side-path and install a fail-closed SessionStart gate, without hand-editing
`~/.claude/settings.json`. The default target is Verdict on **`:8000`**, not
OmniRoute on `:20128`.

Topology:

```
Claude Code  →  Verdict (:8000)  →  OmniRoute (:20128)
                 admit + receipt      execute chosen model
```

## Scope (partial certification)

Claude Code's default traffic is Anthropic Messages (`/v1/messages`). Verdict's
proxy today is OpenAI-compatible only — full Anthropic passthrough is tracked as
. Until then this adapter:

1. Sets `env.OPENAI_BASE_URL` → `http://127.0.0.1:8000/v1`
2. Ensures SessionStart runs `verdict hook claude-gate`
3. Does **not** set `ANTHROPIC_BASE_URL` (wrong protocol for Verdict today)
4. Never writes token values (export `LLMGATE_AUTH_TOKEN` / `OPENAI_API_KEY` yourself)

Live enable with secrets is **NEEDS_OWNER**.

## Enable

Start Verdict first (`verdict serve --host 127.0.0.1 --port 8000`), then:

```bash
verdict harness claude enable
# optional:
#   --base-url http://127.0.0.1:8000/v1
#   --token-env LLMGATE_AUTH_TOKEN
#   --force
```

`enable` will:

1. Health-check Verdict (`/health`, then `/v1/models`) unless `--force`.
2. Copy `~/.claude/settings.json` → `settings.json.verdict.bak` before writing.
3. Upsert `env.OPENAI_BASE_URL` and Verdict harness markers.
4. Append SessionStart `verdict hook claude-gate` when missing.

## Discover / status / certify

```bash
verdict harness claude discover
verdict harness claude status
verdict harness claude certify
```

Status/certify print whether the token env is set (`yes`/`no`) — never the value.
Certify overall level is **partial** while Messages proxy remains unsupported.

## Disable

```bash
verdict harness claude disable
```

Restores the pre-enable backup byte-for-byte.

See also [coding-agent-gate.md](coding-agent-gate.md) and [codex-harness.md](codex-harness.md).
