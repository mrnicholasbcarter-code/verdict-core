# Tasks

## 1. Rewrite OpenSpec change

Verification: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/autodev-live-planner-selection` and `npx --yes @fission-ai/openspec@1.13.2 validate autodev-live-planner-selection --strict --no-interactive` both exit 0. Test: validators pass.

- [x] 1.1 Rename `openspec/changes/autodev-planner-subscription-default` to `openspec/changes/autodev-live-planner-selection`.
- [x] 1.2 Rewrite `proposal.md` to reflect live ladder selection, D22 lift, fail-closed behavior.
- [x] 1.3 Rewrite `design.md` with implementation decisions.
- [x] 1.4 Rewrite `tasks.md` with implementation tasks.
- [x] 1.5 Rewrite `specs/autodev-live-planner-selection/spec.md` (was `autodev-planner-default`) as complete spec delta.
- [x] 1.6 Make the delta ADDED-only (no capability exists on main) and delete the stale `specs/autodev-planner-default/spec.md`; `openspec archive` in a temp worktree succeeds.

## 2. Tests first (TDD - Red phase)

**Focus: `tests/test_decomposer.py`**

- [x] 2.1 Add test: dry run with no decision calls live planner selection and passes chosen model to `DecompositionConfig`.
- [x] 2.2 Add test: dry run with empty ladder fails closed with named error and sends nothing.
- [x] 2.3 Add test: explicit `--orchestrator-model` that is paid is refused with named error.
- [x] 2.4 Update test: `cc/*` planner is no longer refused by decomposer (remove/update `test_cc_planner_excluded`).
- [x] 2.5 Update test: `_live_planner_models` no longer excludes `claude`/`codex` pools (fixture with `claude` pool route can be chosen).

**Focus: `tests/test_autodev_wiring.py`**

- [x] 2.6 Update test: `test_prepare_live_autodev_selected_ladder_spans_free_and_subscription_families` - remove D22 exclusion assertion, allow `claude`/`codex` pool routes in candidates.
- [x] 2.7 Update test: comment at line 2036 about D22 - remove or update.
- [x] 2.8 Add test: explicit `--orchestrator-model` validation through admission (mocked in `test_cli_dry_run_plans_without_launch_authority`).

**Focus: Other test files asserting D22/cc exclusion**

- [x] 2.9 `tests/test_orch_planner.py` - confirm `startswith('cc/')` hits are unrelated route fixtures; leave unchanged.
- [x] 2.10 `tests/test_orch_review.py` - confirm `startswith('cc/')` hits are unrelated; leave unchanged.
- [x] 2.11 `tests/test_orch_runtime.py` - confirm `startswith('cc/')` hits are unrelated; leave unchanged.
- [x] 2.12 `tests/test_orchestration_eligibility_output.py` - confirm `startswith('cc/')` hits are unrelated; leave unchanged.

## 3. Implementation (Green phase)

Verification: focused tests pass (`uv run python -m pytest -q tests/test_decomposer.py tests/test_autodev_wiring.py`), ruff check/format pass, mypy strict pass.

**`verdict/decomposer.py`**

- [x] 3.1 Remove `DEFAULT_ORCHESTRATOR_MODEL` constant (line 35).
- [x] 3.2 Remove `cc/*` planner refusal in `decompose()` (lines 161-165).
- [x] 3.3 Change `DecompositionConfig.model` default to `None` (line 98).
- [x] 3.4 Update any references to `DEFAULT_ORCHESTRATOR_MODEL` in the file.

**`verdict/cli.py`**

- [x] 3.5 Wire dry-run path to `_live_planner_models` when no execution-path decision and no `--orchestrator-model`.
- [x] 3.6 Add admission validation for explicit `--orchestrator-model`.
- [x] 3.7 Remove import of `DEFAULT_ORCHESTRATOR_MODEL` (line 1476).
- [x] 3.8 Handle ladder selection failure (fail closed with named error).

**`verdict/autodev_run.py`**

- [x] 3.9 Remove `_PLANNER_EXCLUDED_POOLS` constant (line 2262).
- [x] 3.10 Remove `exclude_pools=_PLANNER_EXCLUDED_POOLS` from `TaskRequirements` in `_live_planner_models` (line 2313).
- [x] 3.11 Remove `pool_of(route_id) in _PLANNER_EXCLUDED_POOLS` check in `_live_planner_models` (line 2332).
- [x] 3.12 Remove `exclude_pools=_PLANNER_EXCLUDED_POOLS` from `TaskRequirements` in `prepare_live_autodev_selected` (line 2387).
- [x] 3.13 Remove `pool_of(route_id) in _PLANNER_EXCLUDED_POOLS` check in `prepare_live_autodev_selected` (line 2438).
- [x] 3.14 Update any other references to `_PLANNER_EXCLUDED_POOLS` (lines 2415, 2438 comments).

**`verdict/commands/` (if autodev parser needs changes)**

- [x] 3.15 Check if `parsers_autodev.py` or `dispatch.py` needs updates for the new behavior.

## 4. Docs updates

- [x] 4.1 `git grep -n 'codestral-latest\|D22\|DEFAULT_ORCHESTRATOR_MODEL' docs verdict --exclude-dir=docs/proof` - find all references.
- [x] 4.2 Update docs to describe only what runs (remove mentions of old default, D22 exclusion).

## 5. Verification gates (all must pass)

- [x] 5.1 `uv run python -m pytest -q tests/test_decomposer.py tests/test_autodev_wiring.py tests/test_autodev*.py <other touched test files> tests/test_visual_system.py`
- [x] 5.2 `uv run ruff check .`
- [x] 5.3 `uv run ruff format --check .`
- [x] 5.4 `uv run mypy verdict --strict`
- [x] 5.5 `uv run python scripts/check_doc_links.py`
- [x] 5.6 `uv run python scripts/check_reachability.py --ratchet-base origin/main` (0 NEW findings)
- [x] 5.7 `uv run python scripts/check_openspec_tasks.py --base origin/main`
- [x] 5.8 `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/autodev-live-planner-selection`
- [x] 5.9 `npx --yes @fission-ai/openspec@1.13.2 validate autodev-live-planner-selection --strict --no-interactive`
- [x] 5.10 `git diff --check origin/main HEAD`

## 6. Post-merge task (remains open)

Verification: operator runs `verdict autodev --objective <small objective> --dry-run --json` on merged main from installed setup; raw output shows planner chosen by live ladder and no HTTP 402. Test: post-merge live proof.

- [x] (post-merge) Install from merged main and run `verdict autodev --objective <small objective> --dry-run --json` on the operator setup; record the planner the live ladder chose and that no HTTP 402 occurred.
  Done 2026-11-21 on installed-main df4cb61c: planner chosen by the live ladder, no HTTP 402. Evidence: docs/proof/dod/autodev-live-planner-selection.json.
