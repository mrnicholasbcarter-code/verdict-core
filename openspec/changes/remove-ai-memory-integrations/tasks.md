# Tasks

## 1. Remove ai-memory harness and plugin

- [x] 1.1 Delete `verdict/harness_hermes_memory.py` (Hermes ai-memory plugin adapter).
- [x] 1.2 Delete `verdict/hermes_plugin/` (bundled plugin files for verdict-memory).
- [x] 1.3 Remove `_preflight_memory_plugin`, `_install_memory_plugin`,
  `_uninstall_memory_plugin`, `GAP_MEMORY_PLUGIN_INSTALL`, `GAP_MEMORY_PLUGIN_UNINSTALL`
  from `verdict/harness_hermes.py`; remove their call sites in `enable()` and
  `disable()`; update certify notes and `mutation_capability` facet.
- [x] 1.4 Delete `tests/test_harness_hermes_memory.py`.
- [x] 1.5 Remove memory-plugin tests from `tests/test_harness_hermes.py`
  (`test_enable_installs_and_disable_uninstalls_memory_plugin`,
  `test_enable_fails_closed_before_config_write_on_plugin_collision`,
  `test_enable_rolls_back_config_when_install_fails_after_write`,
  `test_disable_restores_config_even_when_plugin_is_not_ours`); simplify
  `test_certify_supported_facets_are_backed_by_live_observed_hooks`.
- [x] 1.6 Remove outbox regression test from `tests/test_shared_memory_redaction.py`
  (the `test_hermes_local_outbox_never_stores_bare_tokens` function).
- [x] 1.7 Remove `verdict/hermes_plugin/verdict_memory/plugin.yaml` from
  `ALLOWED_PACKAGE_FILES` in `tests/test_wheel_contents.py`.
  Verification: `uv run python -m pytest -q tests/test_harness_hermes.py
  tests/test_shared_memory_redaction.py 2>&1 | tail -3` → all pass.

## 2. Remove AI_MEMORY_AUTH_TOKEN credential

- [x] 2.1 Remove `AI_MEMORY_AUTH_TOKEN` `CredentialSpec` from
  `verdict/credentials_registry.py`.
- [x] 2.2 Remove `AI_MEMORY_AUTH_TOKEN` entry from
  `tests/fixtures/actions_golden/credentials_list__default.json`.
  Note: no OpenSpec spec covers the credentials-list command directly;
  the golden fixture and drift tests enforce this change without a spec delta.
  Verification: `uv run python -m pytest -q tests/test_actions_json_golden.py
  tests/test_credentials_drift.py 2>&1 | tail -3` → all pass.

## 3. Remove setup-wizard ai-memory offer

- [x] 3.1 Remove the ai-memory tuple from the optional-tools loop in
  `verdict/home_setup_render.py`.
  Verification: `uv run python -m pytest -q tests/test_visual_system.py
  tests/test_setup_host.py 2>&1 | tail -3` → all pass.

## 4. Remove ai-memory recall evaluator

- [x] 4.1 Delete `scripts/memory_recall_eval.py` (ai-memory recall eval script).
- [x] 4.2 Delete `tests/test_memory_recall_eval.py`.

## 5. Update docs and decisions

- [x] 5.1 `ETHOS.md`: update NOW to say MEM2 is in progress.
- [x] 5.2 `docs/decisions/LOG.md`: add D34 (MEM2 completes the ai-memory decommission).

## 6. Gates

- [x] 6.1 `uv run ruff check . && uv run ruff format --check .` → no findings.
- [x] 6.2 `uv run mypy verdict --strict` → no errors.
- [x] 6.3 `uv run python scripts/check_doc_links.py` → no broken links.
- [x] 6.4 `uv run python scripts/check_reachability.py --ratchet-base origin/main` → no
  new findings, no orphan modules.
- [x] 6.5 `uv run python scripts/check_lint_gates.py` → all gates pass.
  Verification: gate output ends `All gates passed.`

## 7. Post-merge live check

- [ ] (post-merge) 7.1 From merged main: `git grep -i ai-memory -- verdict/` returns
  no hits (operator policy files and ADR history excepted).
- [ ] (post-merge) 7.2 `verdict credentials list` does not list `AI_MEMORY_AUTH_TOKEN`.
