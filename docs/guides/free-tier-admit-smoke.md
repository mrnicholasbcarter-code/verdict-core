# Live-smoke: free-tier ∩ active-provider `verdict route`

Offloadable / low-criticality `verdict route` admits a **concrete**
`free-tier ∩ active-provider` identity, emits a receipt with named drops, then
executes through OmniRoute `POST /v1/chat/completions`. An empty intersection
fails closed (`decision=denied`, model `no_eligible_target`) instead of treating
frontier Opus as a successful offload.

## Prerequisites

OmniRoute reachable at loopback `20128` (SSH tunnel is fine):

```bash
ssh -f -N -L 127.0.0.1:20128:127.0.0.1:20128 \
  -i /workspace/verdict-prime-ssh/id_ed25519 \
  -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes \
  root@172.104.20.227

export OMNIROUTE_BASE_URL=http://127.0.0.1:20128
# Never print the token. Load it into the env only.
export OMNIROUTE_API_KEY="$(ssh -i /workspace/verdict-prime-ssh/id_ed25519 \
  -o StrictHostKeyChecking=no root@172.104.20.227 \
  'cat /root/omniroute-smoke.token')"
```

Confirm the surfaces used by admit:

```bash
curl -sf -H "Authorization: Bearer $OMNIROUTE_API_KEY" \
  "$OMNIROUTE_BASE_URL/api/health"
curl -sf -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $OMNIROUTE_API_KEY" \
  "$OMNIROUTE_BASE_URL/api/free-tier/summary"   # expect 200
curl -sf -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $OMNIROUTE_API_KEY" \
  "$OMNIROUTE_BASE_URL/api/free-tier"           # expect 404
curl -sf -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $OMNIROUTE_API_KEY" \
  "$OMNIROUTE_BASE_URL/api/providers"           # isActive on connections[]
```

## Route smoke

From a verdict-core checkout:

```bash
uv run python -m verdict route "Summarize this sentence in five words." --criticality low
```

Expect:

- **Model** is a concrete catalog identity (not `anthropic/claude-3-opus-20240229`, not `auto/*`)
- **Outcome** `selected` (or `denied` if the live intersection is genuinely empty)
- **Transport** `sent` on a successful execute
- JSON blob includes `admit_receipt` with `admitted`, named `exclusions`
  (`inactive_unconnected`, `not_free_tier`, `metadata_ghost`, `opaque_auto`),
  and `chosen`
- **Strategy** `DIRECT` for low-criticality work (not `SWARM_AUTODEV` from a
  fake tier-0 Opus fallback)

Critical work is unchanged: `verdict route "deploy production infrastructure" --criticality critical`
still never offloads.

## Harness proxy smoke (`verdict serve`)

With the same tunnel + env as above, start Verdict as the harness base URL:

```bash
export LLMGATE_ALLOW_ANONYMOUS=true
uv run python -m verdict serve --host 127.0.0.1 --port 8000
```

In another shell (same `OMNIROUTE_*` env is **not** required for the client —
only Verdict needs it):

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in three words."}],"max_tokens":32,"criticality":"low"}'
```

Expect:

- Upstream call is made by Verdict → OmniRoute only (no public Anthropic/OpenAI)
- Response header `x-verdict-model` is a concrete free∩active identity
- Empty intersection / OmniRoute down → HTTP 503, fail-closed
