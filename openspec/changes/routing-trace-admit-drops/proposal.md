# Proposal: routing-trace-admit-drops

## Problem

`POST /v1/route?include_routing_trace=true` returns a `routing_trace` object
with `admitted_candidate_ids` and `rejected_offers`.  Candidates offered in the
request that were dropped at the admission or confirmation ladder (e.g. by
`gate_admit_prove_confirm` for missing passports) appear in neither field.  For
a request with cc/* candidates, these routes simply vanish from the trace with
no code.

## Change

Add an `admit_drops` field to `routing_trace`.  It lists every offered candidate
that is not in `admitted_candidate_ids`, together with the reason code from the
admission or confirmation ladder.

## Invariant

```
admitted_candidate_ids  ∪  {d.candidate_id for d in admit_drops}
    == set of all offered routes
admitted_candidate_ids  ∩  {d.candidate_id for d in admit_drops}
    == ∅
```

## Implementation

`verdict/intelligence.py` `_prepare_execution_path_request_at` already has
`confirmed.exclusions` (a tuple of `NamedDrop` objects with `.model_id` and
`.reason`).  Build a lookup from that and iterate `raw_offered` to collect
every route not in `admitted_candidate_ids`.

Routes not reached by `gate_admit_prove_confirm` (e.g. filtered by the live
selector before the admission receipt) carry the fallback code `not_admitted`.
