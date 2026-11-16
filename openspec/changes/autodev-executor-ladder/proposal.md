# Proposal: keep the autodev executor ladder at two or more admitted routes

## Why

Unpinned `verdict autodev --allow-live` picks three routes from the eligibility ladder and then probes them. A route that fails its probe is dropped and nothing replaces it, so two failed probes leave one executor and no retry route. On 2026-11-13 a run on installed main ended with one candidate. One route was refused with HTTP 429. A second was dropped as `identity`. A repeat call to that route returned the selected provider and the selected model, with the model id carrying the provider prefix. The probe compares the served-model header with the route id minus its first segment, so it rejects that form. The same selection also spent ladder probes on the `claude` pool, which autodev never uses.

## What Changes

- The executor ladder counts routes that passed their probe. It walks the eligible routes in rank order and stops at three admitted routes. The limit of three is the existing one; it now bounds admitted routes instead of attempted routes.
- The walk is bounded two ways. A model family is probed once per selection, as today. A selection sends at most eight executor probes, the same number the eligibility ladder already allows itself for confirmation probes in one selection.
- The executor probe accepts the served-model header in both forms the gateway reports: the provider-local model id, or the whole selected route id. The executor already accepts both forms when it checks a launched attempt. The probe compares exactly and makes no exception for an opaque alias. The pinned `--executor-model` probe uses the same rule.
- The executor and the planner ladder requests name the `claude` and `codex` pools as excluded, so the ladder does not refresh or confirm routes autodev discards afterwards. The 2026-11-13 run made two selection sweeps about a minute apart. The first stamped five `claude`-pool routes and the second stamped none; by the order of the code the first is the executor selection and the second the planner selection. If only the executor request excluded the pools, those routes would still be stale when the planner selection runs, and the planner request would still list them. That they would then be probed there is an inference, not a test result.
- Unchanged: a non-zero reported response cost still drops a route, a cache hit never admits a route, metered routes and premium-reserved models are never probed, and there is no paid or frontier fallback.

## Capabilities

### New Capabilities

- `autodev-executor-ladder`: how many admitted routes the executor ladder holds, how far it walks to find them, and which served-model values a probe accepts.

### Modified Capabilities

## Impact

`verdict/autodev_run.py` (the pinned and the selected live preparation, and the planner ladder request) and `tests/test_autodev_wiring.py`. No change to the eligibility ladder, the health cache, the shared served-route check, the optimizer or the receipt shape.

A selection may now send up to eight executor probes where it sent three; each is the same bounded probe on a free or subscription route. Excluding the two pools removes them from the routes the ladder may refresh or confirm. Whether the freed probes go unspent or move to other candidates was not measured.

Not changed here, and still open:

- A credential pool that refuses a probe with a rate limit can still be probed for another model family in the same selection.
- The probe checks the served model only, not the serving provider. The shared provider-and-model check was not adopted because it reports a mismatch for `agy/*` routes, which the gateway serves under the provider name `antigravity`.
- A route that Verdict classes free can report a non-zero response cost and is then dropped. `agy/gemini-3.1-flash-lite` did so on 2026-11-13.
- The unarchived change `ad03-autodev-selects` still says "at most the top three" and "FREE" only. That text has not matched the code since the ladder began admitting subscription routes. It belongs to another story and is not edited here.
