# Data Model: Live Routing Golden Path

## Gateway

| Field | Rule |
|-------|------|
| `gateway_id` | Stable id (`omniroute-local`, `provided-catalog`, …) |
| `transport` | How identities are listed and later executed |
| `reachable` | True only from a current reachability check |

A gateway is not a model and not the policy authority.

## Provider

| Field | Rule |
|-------|------|
| `provider_id` | Connection/backend id behind a gateway |
| `gateway_id` | Parent gateway |
| `auth_class` | `oauth` / `api_key` / `local` / `none` — never the secret |

## ConcreteIdentity

| Field | Rule |
|-------|------|
| `identity_id` | Resolved model id, never an alias |
| `provider_id` | Owning provider |
| `cost_class` | `local` / `free` / `cheaper` / `paid` from fetched pricing; else unclassified. Selection rank: local, free, cheaper, paid; ties on `identity_id`. |
| `context_limit` | Fetched or unclassified |
| `output_limit` | Fetched or unclassified |
| `tools` | Fetched boolean or unclassified |
| `modalities` | Fetched set or unclassified |
| `spec_captured_at` | Catalog capture time |
| `fresh_until` | `spec_captured_at + freshness_window` |
| `classification` | `classified` only if every required field is fetched and fresh |

Unclassified identities are not candidates.

## Mix

| Field | Rule |
|-------|------|
| `mix_id` | Combo/chain id |
| `steps` | Ordered list of `identity_id`s; every step named |
| `opaque` | If true, drop the mix |
| `cost_class` | Cost class of the first remaining qualified step |

Qualified iff every named step is a classified, independently qualified identity.

## Candidate

| Field | Rule |
|-------|------|
| `ref` | Identity or mix |
| `status` | `kept` / `dropped` |
| `reason` | Required on drop: `policy` / `health` / `capability` / `unclassified` / `stale` / `opaque_mix` / `cost` |

## RouteSelection

| Field | Rule |
|-------|------|
| `chosen` | Kept candidate |
| `paid_used` | True only if chosen cost class is `paid` |
| `cheaper_available` | True if any kept cheaper/free/local existed |
| Invariant | `paid_used && cheaper_available` is illegal |

## BoundedUnit

| Field | Rule |
|-------|------|
| `unit_id` | Named check |
| `artifact` | Required output |
| `checker` | Known independent pass/fail |
| `attempts` | Unique identities tried, cheaper-first, no retries |

## Receipt

Allowlisted facts only: unit, live endpoint, chosen route, each attempt, cheaper-vs-paid, checker outcome, catalog capture/freshness, source identity. No secrets, prompts, or completions.

## Live surface

Reachable gateway or provider used as both catalog source and execution transport. If unreachable, the run is `live_surface_blocked` and is not a pass. Fixture catalogs do not satisfy this entity.

## Transitions

`listed → specs_fetched → classified | unclassified → probed → kept | dropped → selected → attempted → passed | failed_over | exhausted`
