# Design — native community roster discovery and qualification

## Context

`verdict/discovery/sources/community.py` parsed the `free-coding-models` roster with a regex that expected double-quoted tuples and rejected any array containing a comment or parenthesis. Against the real upstream file it returned zero rows for both the pinned commit and the current one, and the collector treated that as an empty success. Nested model IDs such as `nvidia/nemotron-...` also lost their provider namespace. The roster never reached `verdict identify`, so Verdict could not see, rank or screen those models.

## Goals / Non-Goals

Goals:

- Parse the real upstream file as data, keep `provider/full-model-id`, and fail visibly on an empty or invalid active roster.
- Fetch the current roster by an immutable commit, and keep the pinned fetch for reproducibility.
- Account for every roster entry against the live gateway inventory and name why an entry is not screened.
- Let the existing bounded native screen qualify matching routes, so a pass lands in the same health cache that eligibility reads.

Non-goals (separate work):

- Other catalog comparisons, onboarding missing providers into OmniRoute, and a scheduled sweep of the whole accessible inventory.
- Capability checks beyond the existing three-turn tool probe (reasoning, long context, structured output).
- A verified coding-task receipt through a discovered route; that is post-merge evidence.

## Decisions

1. **Static parser, never execution.** Comments are removed while string literals are kept, then flat tuples of strings, numbers and `null`, and the `sources` provider map, are matched member by member. Unexpected syntax makes that declaration fail; an unknown `models` reference or an empty result makes the fetch raise `ValueError`.
2. **Current revision by commit SHA.** `fetch_fcm_current` reads the latest commit that touched `sources.js` from the GitHub API, requires a 40-hex SHA, and fetches that exact revision. `fetch_fcm` keeps its pinned default.
3. **Exact joins only.** A roster entry joins a gateway route when the provider (with three named spelling aliases: `cloudflare`, `kilo`, `ovhcloud`) and the full model ID match the route's model or its catalog `root`. No leaf or fuzzy matching.
4. **A list price is not a capability verdict.** In a roster screen, a route classified `metered` only by a positive catalog price is skipped as `skipped_spend_policy` unless the operator passes `--assume-unmetered-connections`. That flag never relabels a route, never overrides `--capacity`, and never retries a recorded billing refusal.
5. **Refusals are scoped to the model that refused.** A payment refusal that names the route's model, with no account-wide wording, is recorded with `failure_scope="route"` and `billing_402_metered`, so sibling models on the same connection stay screenable. Account-wide refusals and rate limits still stop the pool.

## Risks / Trade-offs

- An upstream format change makes the roster fetch fail. This is deliberate: the report shows `roster_source_error` instead of a silent empty catalog.
- The GitHub API allows 60 unauthenticated requests per hour. The roster is read only on an explicit `--community-roster` run; no timer uses it.
- A provider that refuses one model by name may still be out of credit account-wide. The cost is one refused probe per sibling, never a paid call.

## Security / trust implications

Upstream data is untrusted. It is never executed, model IDs must match a strict character set, fetches are size-bounded and refuse redirects, and the GitHub response is used only for a validated SHA. Roster membership grants no capacity class, reachability, entitlement or tool proof.

## Routing / context / memory implications

Routing changes only through evidence: a roster screen pass writes the same agentic proof as any identify screen, and eligibility reads it as before. A roster match alone never makes a route eligible, and the report keeps `dispatch_authorized: false`.

## Migration / rollback

All new behaviour is behind `--community-roster`. Rolling back removes the flags; existing health-cache entries stay valid. The one shared change, route-scoped billing refusals in `_detail_is_model_scoped`, can be reverted on its own.

## ADR impact

None. Consistent with ADR-049 (a name or list price alone does not make a route paid) and ADR-012 (live probes need explicit consent and bounds).
