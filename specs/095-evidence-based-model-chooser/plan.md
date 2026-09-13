# Implementation Plan: Evidence-based model chooser (BOD-95)

**Branch**: `feat/bod-95-model-chooser` @ `1c78a96` (`origin/main`)
**Worktree**: `/home/nick/dev/.worktrees/bod-95-model-chooser`
**Linear**: BOD-95
**Spec**: `specs/095-evidence-based-model-chooser/spec.md`

## Technical Context

Reuse brownfield path:

- `verdict/autodev_routing.py`: `CandidateEvidence`, `select_eligible_route`, `EligibilityGate`
- `verdict/eligibility.py`: sole hard admission authority
- `verdict/gateway_adapters.py`: `AdapterRouteIdentity`
- `verdict/cli.py`: add `choose` beside `models`/`inspect`
- Tests: extend `tests/test_autodev_operational_routing.py` and add `tests/test_choose_cli.py`

Do not create a second router. Do not change EligibilityGate policy. Do not add SONA/learning.

## Constitution Check

- Deterministic eligibility before advisory ranking.
- Fail closed on explicit ineligible model and empty admitted set.
- Receipts are secret-free.
- Python tests prove the ten BOD-95 cases.

## Design

1. Add `ResourceClass` and `task_class_is_protected()`.
2. Add `production_ranker(task_class, explicit_model=None)` returning a key for `select_eligible_route`.
   Ranking key, lower is better after max-key inversion or use a higher-is-better tuple:
   - explicit eligible match first
   - protected tasks prefer `subscription_premium`
   - ordinary tasks prefer `free` then `subscription_worker` then `local` then premium/metered
   - within class: observed health/headroom/freshness; UNKNOWN stays UNKNOWN and does not outrank observed healthy values
3. Identity in receipts: `{gateway_id, provider, resource_pool, model_id, route_id}`.
   Map resource_pool from candidate evidence/config, never from brand prefix.
4. `choose_route(candidates, *, task_class, requires, explicit_model)`:
   - filter requires via existing capability_status
   - call select_eligible_route with production ranker
   - catch empty-eligible ValueError into structured no-eligible-target receipt
   - explicit ineligible: if explicit model present but not admitted, fail closed before substituting another candidate
5. CLI `verdict choose --task-class --requires --model --json --candidates-json` (fixture/file for tests and Prime).
   Human output: `selected because ...` plus top fallback and exclusion reasons.
6. Proof: unit fixtures for 10 cases; CLI JSON snapshot; one real dispatch using chooser JSON in this delivery session after local proof.

## Project Structure

Create:

- `verdict/chooser.py` — task-class, resource-class ranker, receipt builder
- `verdict/cli.py` — `cmd_choose` + parser
- `tests/test_chooser.py` — 10 proof cases
- `tests/test_choose_cli.py` — CLI JSON/human output
- `specs/095-evidence-based-model-chooser/*`

Do not modify EligibilityGate internals unless a bug blocks reuse.

## Proof Contract

- STATIC: `ruff check` on touched files; `mypy verdict/chooser.py` if package mypy is practical
- UNIT: `uv run pytest -q tests/test_chooser.py tests/test_choose_cli.py tests/test_autodev_operational_routing.py tests/test_eligibility_gate.py`
- INTEGRATION: `verdict choose --task-class implementation --json` against fixture candidates
- ACCEPTANCE-PROOF: all 10 BOD-95 cases + receipt fields present
