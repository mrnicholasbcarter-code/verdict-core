# Coding-agent gate (Claude Code, Codex, Cursor)

Paste this in under two minutes. Verdict is not a coding agent. It sits in front of
the one you already use, **admits free-tier ∩ active-provider identities**, and
**blocks spend** when the catalog is not qualified or the intersection is empty.

Need: `pip install verdict-core` (or a checkout), OmniRoute on
`http://127.0.0.1:20128`, and **`verdict serve`** as the harness base URL.

## Topology (fail-closed)

```
harness  →  Verdict (:8000)  →  OmniRoute (:20128)
              admit + receipt      execute chosen model
```

- Point the harness at **Verdict**, not OmniRoute.
- Point Verdict at OmniRoute with `OMNIROUTE_BASE_URL` (and `OMNIROUTE_API_KEY`).
- If Verdict or OmniRoute is down, or admit denies, the harness must **not** fall
  back to public Anthropic/OpenAI.

## 0. Start Verdict in front of OmniRoute

```bash
export OMNIROUTE_BASE_URL=http://127.0.0.1:20128
# Never print the token. Load it into the env only.
export OMNIROUTE_API_KEY=…   # from your OmniRoute smoke token
export LLMGATE_ALLOW_ANONYMOUS=true   # local loopback only
verdict serve --host 127.0.0.1 --port 8000
```

`verdict serve` exposes OpenAI-compatible `POST /v1/chat/completions`. On each
request it runs free∩active admit (named drops on the receipt), then forwards
the chosen **concrete** identity through OmniRoute. Empty intersection → HTTP
503, no upstream call.

Optional explicit upstream override: `LLMGATE_UPSTREAM_BASE_URL`
(defaults to `$OMNIROUTE_BASE_URL/v1` when OmniRoute is configured).

## 1. Point the agent at Verdict (OpenAI-compatible)

This is the supported end-to-end path today.

Codex — prefer the Architect-locked CLI (backs up `~/.codex/config.toml` first):

```bash
verdict harness codex enable
verdict harness codex status
verdict harness codex disable
```

See [codex-harness.md](codex-harness.md). Equivalent manual snippet:

```toml
model_provider = "verdict"

[model_providers.verdict]
base_url = "http://127.0.0.1:8000/v1"
env_key = "LLMGATE_AUTH_TOKEN"
wire_api = "responses"
requires_openai_auth = false
```

Cursor — prefer the Architect-locked CLI (managed provider file + OpenAI sidecar):

```bash
verdict harness cursor enable
verdict harness cursor status
verdict harness cursor certify
verdict harness cursor disable
```

See [cursor-harness.md](cursor-harness.md). Manual fallback: set the OpenAI-compatible
base URL to `http://127.0.0.1:8000/v1` in Cursor Models settings.

Harness-shaped smoke (with Verdict running):

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"ping"}],"max_tokens":16,"criticality":"low"}'
```

Expect a concrete admitted model on `x-verdict-model` (not public Anthropic/OpenAI),
or HTTP 503 with fail-closed deny when the intersection is empty / OmniRoute is down.

## 2. Claude Code SessionStart gate

Prefer the Architect-locked CLI (backs up `~/.claude/settings.json` first):

```bash
verdict harness claude enable
verdict harness claude status
verdict harness claude certify
verdict harness claude disable
```

See [claude-harness.md](claude-harness.md).

Claude Code's default traffic is Anthropic Messages (`/v1/messages`). Verdict's
proxy today is OpenAI-compatible only — full Anthropic Messages passthrough via
Verdict is a **remaining gap** (BOD-102). Until then:

1. Keep **SessionStart** fail-closed via `verdict hook claude-gate` (installed by
   `verdict harness claude enable`).
2. Route any OpenAI-compatible side tooling at `http://127.0.0.1:8000/v1`.
3. Do **not** fall back to `api.anthropic.com` if the local path is down.

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

Copy the full file from
[`examples/claude-code/settings.json`](../../examples/claude-code/settings.json).

`verdict hook claude-gate` runs `verdict catalog --management`. Exit **2** if the
catalog is not qualified (`catalog_fetch_timeout`, empty, or `passed: false`).
Claude Code treats exit 2 as a block.

Prove it:

```bash
verdict hook claude-gate
echo $?   # 0 = admitted, 2 = blocked
```

## 3. If OmniRoute is not running

```bash
# start OmniRoute on :20128, then:
verdict detect --json
verdict catalog --management --json
```

`passed: false` or `catalog_fetch_timeout` means **blocked**, not success. See
[unknown ≠ healthy](unknown-not-healthy.md).

Live admit + execute smoke (CLI, not harness):
[free-tier-admit-smoke.md](free-tier-admit-smoke.md).

Background prove-at-rest (free∩active only):
[prove-at-rest-smoke.md](prove-at-rest-smoke.md).

Serve admit passport ∩ confirm gate:
[admit-prove-confirm-smoke.md](admit-prove-confirm-smoke.md).
