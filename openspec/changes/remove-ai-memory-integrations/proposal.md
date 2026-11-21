# Proposal: remove the remaining ai-memory integrations (MEM2)

## Why

POSITIVE. The operator replaced ai-memory with Hindsight on 2026-11-21 (ADR-050).
MEM1 (#1253) removed the reference-docs sync. The remaining ai-memory surface—the
Hermes per-turn memory harness, its plugin, the `AI_MEMORY_AUTH_TOKEN` credential,
and the setup-wizard offer—is now dead code. Removing it eliminates the last ai-memory
references from `verdict/` and completes the full decommission.

## What Changes

- Deleted: `verdict/harness_hermes_memory.py`, `verdict/hermes_plugin/` (plugin
  bundle), `scripts/memory_recall_eval.py`, `tests/test_harness_hermes_memory.py`,
  `tests/test_memory_recall_eval.py`.
- Removed credential: `AI_MEMORY_AUTH_TOKEN` entry removed from
  `verdict/credentials_registry.py` and its golden fixture.
- Removed wizard offer: the ai-memory row removed from the `verdict setup` optional-
  tools loop in `verdict/home_setup_render.py`.
- Removed harness wiring: `verdict harness hermes enable/disable` no longer installs
  or removes the Verdict memory plugin from Hermes config.
- Updated tests: `test_harness_hermes.py` simplified (memory-plugin tests removed);
  `test_shared_memory_redaction.py` drops the outbox-coupled regression test;
  `test_wheel_contents.py` removes the plugin.yaml allowlist entry.
- New: this OpenSpec change, D34 in `docs/decisions/LOG.md`, updated `ETHOS.md`.

## Capabilities

### Modified Capabilities

The `setup-host-prerequisites` capability is modified: the ai-memory offer no longer
appears in the `verdict setup` optional-tools loop.

The `diagnostic-credential-names` capability is modified: `AI_MEMORY_AUTH_TOKEN` is
no longer listed in the credentials registry or in `verdict credentials list` output.

## Impact

- Deleted: `verdict/harness_hermes_memory.py`, `verdict/hermes_plugin/`,
  `scripts/memory_recall_eval.py`, `tests/test_harness_hermes_memory.py`,
  `tests/test_memory_recall_eval.py`.
- Changed: `verdict/harness_hermes.py`, `verdict/credentials_registry.py`,
  `verdict/home_setup_render.py`, `tests/test_harness_hermes.py`,
  `tests/test_shared_memory_redaction.py`, `tests/test_wheel_contents.py`,
  `tests/fixtures/actions_golden/credentials_list__default.json`.
- No new dependency added or removed.

## Evidence boundary

**On branch** — gates pass on `feat/mem2-remove-ai-memory-integrations`. Post-merge
acceptance: `git grep -i ai-memory -- verdict/` returns no hits; `verdict credentials
list` does not list `AI_MEMORY_AUTH_TOKEN`.
