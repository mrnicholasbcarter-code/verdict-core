# Contract: context-intelligence/v1

Public surface for Feature 336. Core owns policy. Memory adapters supply units only.

## Run request

```json
{
  "schema_version": "context-intelligence/v1",
  "task": "Return the unique lift token stored in this project's docs, code, or memory.",
  "gateway_base_url": "http://localhost:20128/v1",
  "proof_root": "isolated directory with planted sources",
  "required_fact": "synthetic planted token (not a secret)"
}
```

Live proof requires a reachable `gateway_base_url` and a cheaper qualified identity with a fetched context limit. Fixture retrieval may test compile rules and MUST NOT emit `conclusion=lift`.

## Slice plan

```json
{
  "slices": [
    {"slice_id": "docs-adr", "category": "docs", "query": "lift token", "root": "docs/adr", "max_units": 8},
    {"slice_id": "code-markers", "category": "code", "query": "lift token", "max_units": 8},
    {"slice_id": "memory-search", "category": "memory", "query": "lift token", "max_units": 8}
  ]
}
```

Forbidden: a slice whose root is a repository root without a tighter query/cap; chat-history dump as a slice.

## Pack receipt (allowlisted)

Must name: included slot keys, each omit/exclude/summarize reason, used tokens, token budget, conflicts, pack digest, whether the required fact was kept.

Must not contain secrets, credentials, raw prompts, completions, or tool arguments.

## Named check

Unaided and packed prompts ask the cheaper identity to reply with only:

```json
{"lift_fact": "<exact planted token>"}
```

The unaided prompt MUST NOT contain the planted token string. The packed attempt MAY include it only inside retrieved units.

Checker passes iff the body parses as JSON and equals that object. Extra keys, prose, markdown fences, or unparseable text fail.

## Paired receipt

Must bind: endpoint, `identity_id`, cost class, pack digest, `unaided_passed`, `packed_passed`, `conclusion`, omissions.

| Unaided | Packed | Surface | Conclusion |
|---------|--------|---------|------------|
| fail | success | live, same identity | `lift` |
| success | success | live, same identity | `no_lift` |
| fail | fail | live, same identity | `no_lift` |
| n/a | n/a | unreachable / no cheaper identity / unknown context / compile refuse | `blocked` |

Invalid pair (different identities, fixture stub): do not report `lift`.

## Errors (stable codes)

- `live_surface_blocked`
- `no_cheaper_identity`
- `unclassified_context_limit`
- `required_fact_missing`
- `required_fact_omitted`
- `repo_dump_refused`
- `secret_refused`
- `invalid_pair`

## Working state vs durable memory

- Working state: typed slots, per attempt, not auto-ingested.
- Durable ingest: `MemoryGate` only; rejected secrets/transcripts stay rejected.
- Search: `MemoryPlane.search` / `search_ranked`; adapters do not change admission.
