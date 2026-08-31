# Context intelligence lift: paired live proof (#336)

A cheaper identity is asked the same named check twice — once unaided, once with a compiled
`ContextPack`. Lift is claimed only when the unaided attempt fails and the packed attempt passes
on the **same** live identity. Fixture catalogs never satisfy this proof; an unreachable gateway
produces `conclusion: "blocked"`, never a silent pass.

## The named check

A synthetic, non-secret token is planted into local docs, local code, and durable memory before
the run. It never appears in the unaided prompt wording. The identity must reply with exactly:

```json
{"lift_fact": "<planted token>"}
```

An independent checker parses that object and accepts only an exact match. Extra keys, prose, or
markdown fail. The token cannot be guessed from the task wording, so a pass requires the pack.

## Reproduce

```python
import json, pathlib
from verdict.context_lift import run_context_lift
from verdict.live_routing_gateway import DEFAULT_GATEWAY

result = run_context_lift(base_url=DEFAULT_GATEWAY, proof_root=pathlib.Path("/tmp/liftproof"))
print(json.dumps(result["receipt"], indent=2, sort_keys=True))
```

Requires an OmniRoute-compatible gateway at `http://localhost:20128/v1`. `proof_root` must be a
scratch directory — the run plants the token and noise files there. The pytest equivalent is
`tests/test_context_lift_live.py`, which skips when the live surface is blocked.

## Recorded receipt

[`context-lift-receipt.json`](./context-lift-receipt.json), captured 2026-08-31:

| Field | Value |
| --- | --- |
| `identity_id` | `kc/kilo-auto/free` |
| `cost_class` | `free` |
| `unaided_passed` | `false` |
| `packed_passed` | `true` |
| `conclusion` | `lift` |
| `omissions` | none |

## Reading the receipt

- **`conclusion`** — `lift` requires `unaided_passed=false` and `packed_passed=true`; `no_lift`
  means the pack did not change the outcome; `blocked` means no claim is made and
  `block_reason` names why (`live_surface_blocked`, `no_cheaper_identity`,
  `unclassified_context_limit`, `execute_disabled`).
- **`cost_class`** — always `local`, `free`, or `cheaper`. A paid identity is never the lift
  subject while a cheaper qualified identity remains.
- **`pack_digest`** — content digest of the compiled pack, so a run is auditable after the fact.
- **`omissions`** — every unit dropped to fit the identity's context limit, with a category and
  reason. Required policy and the planted fact are never dropped; if they cannot fit, the run
  fails closed rather than sending a truncated pack.

Receipts pass through secret stripping before serialization, so no credentials, prompts, or
completions are recorded.
