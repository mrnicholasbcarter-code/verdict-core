# OpenCode harness: point OpenCode at Verdict

Architect lock: `verdict harness opencode discover|enable|disable|status|certify`.

These commands switch OpenCode onto Verdict as an OpenAI-compatible custom
provider without hand-editing `~/.config/opencode/opencode.json`. The default
target is Verdict on **`:8000`**, not OmniRoute on `:20128`.

Topology:

```
OpenCode  →  Verdict (:8000)  →  OmniRoute (:20128)
               admit + receipt      execute chosen model
```

## Scope (partial / not-installed)

- Config path: `~/.config/opencode/opencode.json` (`provider.verdict`).
- Uses `@ai-sdk/openai-compatible` with `options.baseURL` → Verdict `:8000`.
- Sets `model` to `verdict/default`.
- Never writes `~/.local/share/opencode/auth.json` secrets.
- Binaries `opencode` / `opencode-go` may be missing; discover/certify still run
  and report **not-installed**.
- Live enable with secrets (`opencode auth login` / token export) is **NEEDS_OWNER**.

## Enable

Start Verdict first (`verdict serve --host 127.0.0.1 --port 8000`), then:

```bash
verdict harness opencode enable
# optional:
#   --base-url http://127.0.0.1:8000/v1
#   --token-env LLMGATE_AUTH_TOKEN
#   --force
```

`enable` will:

1. Health-check Verdict (`/health`, then `/v1/models`) unless `--force`.
2. Copy `opencode.json` → `opencode.json.verdict.bak` before writing.
3. Upsert `provider.verdict` and set `model` to `verdict/default`.
4. Refuse OmniRoute-looking `:20128` base URLs unless `--force`.

## Discover / status / certify

```bash
verdict harness opencode discover
verdict harness opencode status
verdict harness opencode certify
```

Status/certify print whether the token env is set (`yes`/`no`) — never the value.

## Disable

```bash
verdict harness opencode disable
```

Restores the pre-enable backup byte-for-byte.

See also [coding-agent-gate.md](coding-agent-gate.md) and [codex-harness.md](codex-harness.md).
