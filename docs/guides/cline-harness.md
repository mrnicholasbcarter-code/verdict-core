# Cline harness: point Cline at Verdict

Architect lock: `verdict harness cline discover|enable|disable|status|certify`.

These commands configure Cline (VS Code extension and/or `cline` npm CLI) for
Verdict as an OpenAI-compatible endpoint. The default target is Verdict on
**`:8000`**, not OmniRoute on `:20128`. OmniRoute is an optional upstream behind
`verdict serve` — never the harness base URL.

Topology:

```
Cline  →  Verdict (:8000)  →  OmniRoute (:20128)   [OmniRoute optional]
           admit + receipt      execute chosen model
```

## Integration preference

1. **Verdict-managed** — `~/.cline/verdict-provider.json` + `verdict-openai.env`
2. **Cline CLI** (when `cline` is on `PATH` or `~/.cline/` already exists) —
   upsert OpenAI-compatible base URL into `~/.cline/data/settings/providers.json`
   (never writes API keys)
3. **IDE settings** (when only VS Code / Code-OSS settings are present) —
   reversible `settings.json` keys (`cline.openAiBaseUrl`, `cline.apiProvider`)
   plus documented UI confirmation steps

Discover/status report **not installed** gracefully when the `cline` binary is
missing.

Certification is **partial**: managed files are reversible and CI-safe; live
IDE API-key paste is **NEEDS_OWNER**.

## Enable

```bash
verdict harness cline enable
# optional:
#   --base-url http://127.0.0.1:8000/v1
#   --token-env LLMGATE_AUTH_TOKEN
#   --force
```

Never prints token values. Export the token env (or paste into Cline's OpenAI
Compatible API key field) yourself.

### IDE UI steps (when CLI is absent)

1. Open Cline in VS Code → Settings (gear) → API Configuration
2. Set API Provider to **OpenAI Compatible**
3. Set Base URL to `http://127.0.0.1:8000/v1`
4. Paste API key from your token env
5. Save, then send a short test message

## Discover / status / certify

```bash
verdict harness cline discover
verdict harness cline status
verdict harness cline certify
```

## Disable

```bash
verdict harness cline disable
```

Restores backups for any file Verdict mutated (provider JSON, env sidecar,
providers.json, settings.json).

See also [coding-agent-gate.md](coding-agent-gate.md) and [codex-harness.md](codex-harness.md).
