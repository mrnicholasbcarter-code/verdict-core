# Coding-agent gate (Claude Code, Codex, Cursor)

Paste this in under two minutes. Verdict is not a coding agent. It sits in front of the one you already use and **blocks spend** when the catalog is not qualified.

Need: `pip install verdict-core` and a local OpenAI-compatible gateway on `http://127.0.0.1:20128` (OmniRoute, or LiteLLM pointed at the same port).

## 1. Point the agent at the gateway

Claude Code (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:20128"
  }
}
```

Copy the full file from [`examples/claude-code/settings.json`](../../examples/claude-code/settings.json).

Codex (`~/.codex/config.toml`):

```toml
[model_providers.omniroute]
name = "omniroute"
base_url = "http://127.0.0.1:20128/v1"
```

Cursor: set the OpenAI-compatible base URL to `http://127.0.0.1:20128/v1` in models settings.

If the gateway is down, the agent should fail to call models. That is fail-closed. Do not fall back to the public Anthropic API.

## 2. Fail closed before a session spends

Same settings file, SessionStart hook:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "verdict",
            "args": ["hook", "claude-gate"],
            "timeout": 70
          }
        ]
      }
    ]
  }
}
```

`verdict hook claude-gate` runs `verdict catalog --management`. Exit **2** if the catalog is not qualified (`catalog_fetch_timeout`, empty, or `passed: false`). Claude Code treats exit 2 as a block.

Prove it:

```bash
verdict hook claude-gate
echo $?   # 0 = admitted, 2 = blocked
```

## 3. If the gateway is not running

```bash
# start your local gateway on :20128, then:
verdict detect --json
verdict catalog --management --json
```

`passed: false` or `catalog_fetch_timeout` means **blocked**, not success. See [unknown ≠ healthy](unknown-not-healthy.md).
