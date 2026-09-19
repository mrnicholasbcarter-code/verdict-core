# Prime Agent harness: point Prime at Verdict

Architect lock: `verdict harness prime discover|enable|disable|status|certify`.

These commands switch Prime Agent onto Verdict as an OpenAI-compatible custom
provider without hand-editing `~/.prime/agent/models.json`. The default target
is Verdict on **`:8000`**, not OmniRoute on `:20128`.

Topology:

```
Prime Agent  →  Verdict (:8000)  →  OmniRoute (:20128)
                  admit + receipt      execute chosen model
```

## Scope (partial / not-installed)

- Config path: `~/.prime/agent/models.json` (`providers.verdict`).
- `apiKey` is the **env var name** (default `LLMGATE_AUTH_TOKEN`) — never a secret.
- Binaries `prime` / `prime-agent` may be missing; discover/certify still run and
  report **not-installed**.
- Live enable with secrets / `/login` / `auth.json` is **NEEDS_OWNER**.

## Enable

Start Verdict first (`verdict serve --host 127.0.0.1 --port 8000`), then:

```bash
verdict harness prime enable
# optional:
#   --base-url http://127.0.0.1:8000/v1
#   --token-env LLMGATE_AUTH_TOKEN
#   --force
```

`enable` will:

1. Health-check Verdict (`/health`, then `/v1/models`) unless `--force`.
2. Copy `models.json` → `models.json.verdict.bak` before writing.
3. Upsert `providers.verdict` with `baseUrl`, `api: openai-completions`, and
   `apiKey` set to the token env **name**.
4. Refuse OmniRoute-looking `:20128` base URLs unless `--force`.

## Discover / status / certify

```bash
verdict harness prime discover
verdict harness prime status
verdict harness prime certify
```

Status/certify print whether the token env is set (`yes`/`no`) — never the value.

## Disable

```bash
verdict harness prime disable
```

Restores the pre-enable backup byte-for-byte.

See also [coding-agent-gate.md](coding-agent-gate.md) and [codex-harness.md](codex-harness.md).
