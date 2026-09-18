# Codex harness: point Codex at Verdict

Architect lock: `verdict harness codex enable|disable|status`.

These commands switch Codex onto Verdict as an OpenAI-compatible provider
without hand-editing `~/.codex/config.toml`. The default target is Verdict on
**`:8000`**, not OmniRoute on `:20128`.

Topology:

```
Codex  →  Verdict (:8000)  →  OmniRoute (:20128)
           admit + receipt      execute chosen model
```

## Enable

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

## Status

```bash
verdict harness codex status
```

Prints the active provider, base URL, and whether the token env is set
(`yes`/`no`). It never prints the token.

## Disable

```bash
verdict harness codex disable
```

Restores `~/.codex/config.toml` from the pre-enable backup. If there is no
backup (Verdict was never enabled through this CLI), the command exits
non-zero with a clear message.

## Remote Verdict over SSH

If Verdict is running on another host, tunnel port 8000 and enable as usual:

```bash
ssh -L 8000:127.0.0.1:8000 user@verdict-host
verdict harness codex enable --base-url http://127.0.0.1:8000/v1
```

See also [coding-agent-gate.md](coding-agent-gate.md) for the fail-closed
harness → Verdict → OmniRoute path.
