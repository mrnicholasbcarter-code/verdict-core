# Smoke: serve admit = free∩active ∩ fresh passport ∩ budgeted confirm

Cheap-path `/v1/chat/completions` (and low-criticality `verdict route`) no longer
admits on OmniRoute `isActive` alone. After free∩active filtering, Core requires:

1. A **fresh prove-at-rest passport** for the identity
2. A **budgeted confirm** probe that succeeds

Expired OAuth/cookie / unproven identities are **named drops before select**
(`no_passport`, `passport_stale`, `confirm_failed`, `confirm_unavailable`, …).
Empty intersection fails closed (`no_eligible_target`) — never Opus false success.

Hand-picked providers are out of scope for this smoke.

## Prerequisites

Same OmniRoute tunnel + env as [free-tier-admit-smoke.md](free-tier-admit-smoke.md).

## 1. Prove at rest once

```bash
export OMNIROUTE_BASE_URL=http://127.0.0.1:20128
export OMNIROUTE_API_KEY=…   # never print

uv run python -m verdict prove-at-rest once --allow-live-probe --json
```

Expect healthy rows under `~/.verdict/prove-at-rest/state.json` (override with
`VERDICT_PROVE_AT_REST_STATE` / `--state-path`). Details:
[prove-at-rest-smoke.md](prove-at-rest-smoke.md).

## 2. Serve (harness path)

```bash
export LLMGATE_ALLOW_ANONYMOUS=true
uv run python -m verdict serve --host 127.0.0.1 --port 8000
```

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in three words."}],"max_tokens":32,"criticality":"low"}'
```

## Receipt fields to assert

On a selected decision, `admit_receipt` (decision / evidence receipt) must include:

| Field | Expect |
| --- | --- |
| `admitted` / `chosen` | Concrete free∩active identity (not Opus, not `auto/*`) |
| `exclusions[].reason` | Named drops: `inactive_unconnected`, `not_free_tier`, `no_passport`, `passport_stale`, `confirm_failed`, … |
| `pack_digest` | Stable `sha256:…` cheap-path pack (workspace provenance units compiled under budget). A digest alone is **not** hydrated. |
| `pack_state` | `empty` \| `partial` \| `hydrated` \| `failed`. **Savings stay blocked until `hydrated`.** Empty/partial with a pretty digest is still a FAIL for rich hydrate. |
| `included` / `included_sources` | Provenance for compiled units (`source_uri` + content digest). QA smokes `included_sources`. ADR and architecture files that exist on disk must appear here for `hydrated`. |
| `omissions` | Named context-pack omissions (missing roots, unreadables, budget excludes for truly oversize units — never invented content) |
| **`passport`** | Per-candidate evidence: `identity_id`, `fresh`, `expires_at`, `auth_state`, … |
| **`confirm`** | Per-candidate evidence: `identity_id`, `confirmed`, `status`, optional `latency_ms` / `error` |

CLI check:

```bash
uv run python -m verdict route "Summarize this sentence in five words." --criticality low
```

Inspect the printed `admit_receipt` for `passport` + `confirm` arrays with at
least one `fresh: true` and `confirmed: true` on the chosen identity.

## Without prove-at-rest

If the passport store is empty/missing, admit fail-closes with `no_passport`
drops — do **not** expect a selected free-tier model until step 1 succeeds.

## Fixture tests (no live OmniRoute)

```bash
uv run pytest tests/test_admit_prove_confirm.py tests/test_free_tier_admit.py tests/test_proxy_free_tier_admit.py -q
```
