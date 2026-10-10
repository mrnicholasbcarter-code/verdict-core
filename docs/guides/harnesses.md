# Coding-agent harnesses: point your tools at Verdict

Each supported harness gets an Architect-locked `verdict harness <name>` CLI
that edits its config in place (with a byte-for-byte reversible backup)
instead of hand-editing tool config files. The default target is always
Verdict on **`:8000`**, not OmniRoute on `:20128`.

Topology for supported OpenAI-compatible traffic (not all harness traffic):

```
harness  →  Verdict (:8000)  →  OmniRoute (:20128)
             admit + receipt      execute chosen model
```

Claude Code's default Anthropic Messages traffic is not proxied by Verdict.
The Claude adapter below covers a side-path and a SessionStart gate only.
Direct provider traffic remains outside this relay boundary.

See [coding-agent-gate.md](coding-agent-gate.md) for the relay's fail-closed
setup, and [controller-routing.md](controller-routing.md) /
[prime-workflow.md](prime-workflow.md) for the Prime Agent supervisor and
autonomous-workflow contracts (Prime's harness adapter is covered here; its
supervisor/lease contract is not).

## Claude Code

Architect lock: `verdict harness claude discover|enable|disable|status|certify`.

These commands switch Claude Code onto Verdict for the **OpenAI-compatible**
side-path and install a fail-closed SessionStart gate, without hand-editing
`~/.claude/settings.json`.

### Scope (partial certification)

Claude Code's default traffic is Anthropic Messages (`/v1/messages`). Verdict's
proxy today is OpenAI-compatible only — full Anthropic passthrough is a
remaining gap. Until then this adapter:

1. Sets `env.OPENAI_BASE_URL` → `http://127.0.0.1:8000/v1`
2. Ensures SessionStart runs `verdict hook claude-gate`
3. Does **not** set `ANTHROPIC_BASE_URL` (wrong protocol for Verdict today)
4. Never writes token values (export `LLMGATE_AUTH_TOKEN` / `OPENAI_API_KEY` yourself)

Live enable with secrets is **NEEDS_OWNER**.

### Enable

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

### Discover / status / certify

```bash
verdict harness claude discover
verdict harness claude status
verdict harness claude certify
```

Status/certify print whether the token env is set (`yes`/`no`) — never the value.
Certify overall level is **partial** while Messages proxy remains unsupported.

### Disable

```bash
verdict harness claude disable
```

Restores the pre-enable backup byte-for-byte.

## Cline

Architect lock: `verdict harness cline discover|enable|disable|status|certify`.

These commands configure Cline (VS Code extension and/or `cline` npm CLI) for
Verdict as an OpenAI-compatible endpoint. OmniRoute is an optional upstream
behind `verdict serve` — never the harness base URL.

### Integration preference

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

### Enable

```bash
verdict harness cline enable
# optional:
#   --base-url http://127.0.0.1:8000/v1
#   --token-env LLMGATE_AUTH_TOKEN
#   --force
```

Never prints token values. Export the token env (or paste into Cline's OpenAI
Compatible API key field) yourself.

#### IDE UI steps (when CLI is absent)

1. Open Cline in VS Code → Settings (gear) → API Configuration
2. Set API Provider to **OpenAI Compatible**
3. Set Base URL to `http://127.0.0.1:8000/v1`
4. Paste API key from your token env
5. Save, then send a short test message

### Discover / status / certify

```bash
verdict harness cline discover
verdict harness cline status
verdict harness cline certify
```

### Disable

```bash
verdict harness cline disable
```

Restores backups for any file Verdict mutated (provider JSON, env sidecar,
providers.json, settings.json).

## Codex

Architect lock: `verdict harness codex enable|disable|status`.

These commands switch Codex onto Verdict as an OpenAI-compatible provider
without hand-editing `~/.codex/config.toml`.

### Enable

Start Verdict first (`verdict serve --host 127.0.0.1 --port 8000`), then:

```bash
verdict harness codex enable
# optional:
#   --base-url http://127.0.0.1:8000/v1
#   --token-env LLMGATE_AUTH_TOKEN
#   --force
```

`enable` will:

1. `GET` health on the Verdict host (`/health`, then `/v1/models`). If both fail,
   it refuses unless you pass `--force`.
2. Copy `~/.codex/config.toml` to `~/.codex/config.toml.verdict.bak` **before**
   writing (stable backup; a later enable does not overwrite it).
3. Write/update `[model_providers.verdict]` with `base_url`, `env_key`,
   `wire_api = "responses"`, and `requires_openai_auth = false`.
4. Set `model_provider = "verdict"`. Other Codex keys are left in place.

The token value is never printed. Codex reads it from the env var named by
`--token-env` (default `LLMGATE_AUTH_TOKEN`).

Enable rewrites the `model_provider` line and the Verdict provider keys.
Untouched keys stay. Comments on those rewritten lines may change; **disable
restores the pre-enable backup byte-for-byte**, including comments.

### Status

```bash
verdict harness codex status
```

Prints the active provider, base URL, and whether the token env is set
(`yes`/`no`). It never prints the token.

### Disable

```bash
verdict harness codex disable
```

Restores `~/.codex/config.toml` from the pre-enable backup. If there is no
backup (Verdict was never enabled through this CLI), the command exits
non-zero with a clear message.

### Remote Verdict over SSH

If Verdict is running on another host, tunnel port 8000 and enable as usual:

```bash
ssh -L 8000:127.0.0.1:8000 user@verdict-host
verdict harness codex enable --base-url http://127.0.0.1:8000/v1
```

## Cursor

Architect lock: `verdict harness cursor discover|enable|disable|status|certify`.

These commands configure Cursor for Verdict as an OpenAI-compatible endpoint
without hand-editing opaque IDE stores.

### Integration preference

1. **Verdict-managed** — `~/.cursor/verdict-provider.json`
2. **OpenAI-compatible** — `~/.cursor/verdict-openai.env` plus optional
   `~/.config/Cursor/User/settings.json` (`openai.baseUrl`)
3. **Wrapper last** — `~/.cursor/bin/cursor-verdict` (pass `--wrapper`, or when
   no settings target exists)

Cursor IDE's "Override OpenAI Base URL" UI often lives in `state.vscdb`. This
adapter therefore certifies as **partial**: managed files are reversible and
CI-safe; live IDE toggle / API-key paste is **NEEDS_OWNER**.

### Enable

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

### Discover / status / certify

```bash
verdict harness cursor discover
verdict harness cursor status
verdict harness cursor certify
```

### Disable

```bash
verdict harness cursor disable
```

Restores backups for any file Verdict mutated (provider JSON, env sidecar,
settings.json, wrapper).

## OpenCode

Architect lock: `verdict harness opencode discover|enable|disable|status|certify`.

These commands switch OpenCode onto Verdict as an OpenAI-compatible custom
provider without hand-editing `~/.config/opencode/opencode.json`.

### Scope (partial / not-installed)

- Config path: `~/.config/opencode/opencode.json` (`provider.verdict`).
- Uses `@ai-sdk/openai-compatible` with `options.baseURL` → Verdict `:8000`.
- Sets `model` to `verdict/default`.
- Never writes `~/.local/share/opencode/auth.json` secrets.
- Binaries `opencode` / `opencode-go` may be missing; discover/certify still run
  and report **not-installed**.
- Live enable with secrets (`opencode auth login` / token export) is **NEEDS_OWNER**.

### Enable

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

### Discover / status / certify

```bash
verdict harness opencode discover
verdict harness opencode status
verdict harness opencode certify
```

Status/certify print whether the token env is set (`yes`/`no`) — never the value.

### Disable

```bash
verdict harness opencode disable
```

Restores the pre-enable backup byte-for-byte.

## Prime Agent

Architect lock: `verdict harness prime discover|enable|disable|status|certify`.

These commands switch Prime Agent onto Verdict as an OpenAI-compatible custom
provider without hand-editing `~/.prime/agent/models.json`.

### Scope (partial / not-installed)

- Config path: `~/.prime/agent/models.json` (`providers.verdict`).
- `apiKey` is the **env var name** (default `LLMGATE_AUTH_TOKEN`) — never a secret.
- Binaries `prime` / `prime-agent` may be missing; discover/certify still run and
  report **not-installed**.
- Live enable with secrets / `/login` / `auth.json` is **NEEDS_OWNER**.

### Enable

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

### Discover / status / certify

```bash
verdict harness prime discover
verdict harness prime status
verdict harness prime certify
```

Status/certify print whether the token env is set (`yes`/`no`) — never the value.

### Disable

```bash
verdict harness prime disable
```

Restores the pre-enable backup byte-for-byte.

### Keeping the Prime model registry current

Verdict refreshes `~/.prime/agent/models.json` from the live OmniRoute catalog
whenever it selects a model (worker runs, `verdict eligibility`,
`verdict orchestrate`). To also refresh it between runs, install the user timer:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/verdict-prime-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now verdict-prime-sync.timer
```

Edit `VERDICT_CHECKOUT` in the service if your checkout is not at
`~/dev/verdict-core`. `sync-models` writes nothing when the model set is
unchanged, and keeps only the newest 5 `models.json.verdict-sync-*.bak` backups.
Check the last run with `journalctl --user -u verdict-prime-sync`.



### Health-cache prober

`verdict prove-at-rest daemon` is the single prober. It probes every admitted
route, spends most of its budget on FREE routes, and writes
the health cache (`$VERDICT_HOME/health-cache.json`, default
`~/.verdict/health-cache.json`). It does not write
`~/.verdict/orchestration-health.json` (the selection ladder still owns that
file). The older `~/.verdict/prove-at-rest/state.json` cycle document is
ignored and left in place.

Install it next to the Prime sync timer. Do not enable it until the unit has
been reviewed:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/verdict-health-cache.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now verdict-health-cache.service
```

Edit `VERDICT_CHECKOUT` if the checkout is not at `~/dev/verdict-core`.
The process probes, sleeps `--interval` seconds (10 minutes in the unit), and
repeats. Each cycle stops at 300 requests or 10 minutes. Check it with
`journalctl --user -u verdict-health-cache` and
`verdict prove-at-rest status`.
