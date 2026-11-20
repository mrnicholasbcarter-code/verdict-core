## ADDED Requirements

### Requirement: routing_trace SHALL name every offered candidate that was dropped, with a code

The `routing_trace` object returned by `POST /v1/route?include_routing_trace=true`
MUST include an `admit_drops` field.  `admit_drops` is a list of objects, each
with keys `candidate_id` (string) and `code` (string).

An entry MUST appear for every route that was offered in the request but is not
present in `admitted_candidate_ids`.  No entry may appear for a route that is in
`admitted_candidate_ids`; the two sets MUST be disjoint.

The `code` field MUST be a non-empty string taken from the admission or
confirmation ladder for that route (e.g. `no_fresh_passport`, `passport_stale`,
`hard_ineligible_route`).  Routes that were filtered before
`gate_admit_prove_confirm` ran carry the fallback code `not_admitted`.

#### Scenario: All offered routes admitted

- **GIVEN** a request whose routes all pass the admission and confirmation ladders
- **THEN** `admit_drops` is an empty list and `admitted_candidate_ids` equals the set of all offered routes

#### Scenario: Some offered routes dropped at confirmation

- **GIVEN** a request where some routes fail `gate_admit_prove_confirm` (e.g. no fresh passport)
- **THEN** each dropped route appears exactly once in `admit_drops` with the reason code from the confirmation ladder (e.g. `no_fresh_passport`)
- **AND** `admitted_candidate_ids` ∪ `{d.candidate_id for d in admit_drops}` equals the offered set
- **AND** `admitted_candidate_ids` ∩ `{d.candidate_id for d in admit_drops}` is empty

#### Scenario: No offered routes admitted

- **GIVEN** a request where all routes fail admission (e.g. all hard-ineligible)
- **THEN** `admit_drops` lists every offered route, `admitted_candidate_ids` is empty
