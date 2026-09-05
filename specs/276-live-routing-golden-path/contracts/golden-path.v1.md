# Contract: golden-path/v1

Public surface for Feature 276. Core owns policy. Gateways execute chosen identities only.

## Run request

```json
{
  "schema_version": "golden-path/v1",
  "unit_id": "named-check",
  "artifact": "path or logical artifact id",
  "checker": "declared independent check",
  "catalog_source": "live-gateway",
  "gateway_base_url": "http://localhost:20128/v1",
  "freshness_seconds": 3600
}
```

- Golden-path demonstration requires `catalog_source=live-gateway` and a reachable `gateway_base_url`.
- `freshness_seconds` is the operator-declared window; default 3600.
- A fixture catalog may be used only in tests of classification rules; those tests MUST NOT emit a golden-path pass receipt.

## Catalog row (after fetch, before probe)

Required: `identity_id`, `provider_id`, `gateway_id`, `spec_captured_at`.  
Required for classification: `cost_class`, `context_limit`, `output_limit`, `tools`, `modalities`.  
Missing required classification fields → unclassified → drop.

Forbidden: treating `auto/*`, unexpanded aliases, or unnamed combo steps as `identity_id`.

## Mix row

```json
{
  "mix_id": "combo-1",
  "opaque": false,
  "steps": ["provider-a/model-1", "provider-b/model-2"]
}
```

`opaque=true` or any unnamed/unclassified step → drop.

## Explanation (operator-visible)

Must name: kept identities, each drop reason, chosen identity or mix, first-step cost class, whether paid was used, whether a cheaper kept candidate existed.

Must not contain secrets, prompts, completions, or raw tool arguments.

## Receipt

Must bind: `unit_id`, catalog capture/freshness, chosen route, attempt list (identity, cost class, checker result), final pass/fail, source identity of the work.

Invariant: if `cheaper_available` was true at first selection, first attempt cost class is not `paid`.

## Usage snapshot (allowlisted)

```json
{
  "provider_id": "codex",
  "source": "oauth-file",
  "used_percent": 0.42,
  "remaining_percent": 0.58,
  "resets_at": "2026-08-30T12:00:00Z",
  "exhausted": false
}
```

No tokens, cookies, emails, or raw auth files. `exhausted=true` ⇒ that provider’s identities cannot stay `free`/`local`/`cheaper`.

## Named check

Prompt the selected identity to reply with only:

```json
{"golden_path": "ok"}
```

Checker passes iff the body parses as JSON and `golden_path` is the string `ok`. Extra keys, prose, markdown fences, or unparseable text fail.

## Failover

Cheaper unused qualified identities first; paid only after that set is empty; no retry of the same `identity_id`; stop on checker pass or exhaustion.

## Errors (stable codes)

`empty_catalog`, `unclassified`, `stale_specs`, `opaque_mix`, `no_qualified_candidate`, `checker_failed`, `exhausted`, `live_surface_blocked`
