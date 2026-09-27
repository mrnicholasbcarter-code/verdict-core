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


## Keeping the Prime model registry current

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
