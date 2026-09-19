# Cursor harness: point Cursor at Verdict

Architect lock: `verdict harness cursor discover|enable|disable|status|certify`.

These commands configure Cursor for Verdict as an OpenAI-compatible endpoint
without hand-editing opaque IDE stores. The default target is Verdict on
**`:8000`**, not OmniRoute on `:20128`.

Topology:

```
Cursor  →  Verdict (:8000)  →  OmniRoute (:20128)
            admit + receipt      execute chosen model
```

## Integration preference

1. **Verdict-managed** — `~/.cursor/verdict-provider.json`
2. **OpenAI-compatible** — `~/.cursor/verdict-openai.env` plus optional
   `~/.config/Cursor/User/settings.json` (`openai.baseUrl`)
3. **Wrapper last** — `~/.cursor/bin/cursor-verdict` (pass `--wrapper`, or when
   no settings target exists)

Cursor IDE's "Override OpenAI Base URL" UI often lives in `state.vscdb`. This
adapter therefore certifies as **partial**: managed files are reversible and
CI-safe; live IDE toggle / API-key paste is **NEEDS_OWNER**.

## Enable

```bash
verdict harness cursor enable
# optional:
#   --base-url http://127.0.0.1:8000/v1
#   --token-env LLMGATE_AUTH_TOKEN
#   --force
#   --wrapper
```

Never prints token values. Export the token env (or paste into Cursor's OpenAI
API key field) yourself.

## Discover / status / certify

```bash
verdict harness cursor discover
verdict harness cursor status
verdict harness cursor certify
```

## Disable

```bash
verdict harness cursor disable
```

Restores backups for any file Verdict mutated (provider JSON, env sidecar,
settings.json, wrapper).

See also [coding-agent-gate.md](coding-agent-gate.md) and [codex-harness.md](codex-harness.md).
