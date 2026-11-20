# Tasks

## 1. Core implementation

Verification: `uv run python -m pytest -q tests/test_fusion_plan_bench.py` passes.

- [x] 1.1 Add `verdict/fusion_plan_bench.py` with panel, synthesis, structural score, cap enforcement, and allowlist-guarded disk output.
- [x] 1.2 Add `verdict bench fusion-plan` subparser in `verdict/commands/parsers_autodev.py` after `context-lift`.
- [x] 1.3 Wire `_cmd_bench_fusion_plan` production caller in `verdict/commands/dispatch.py`.
- [x] 1.4 Add goals fixture `benchmarks/fixtures/fusion-plan-bench-goals.json` (no host paths).

## 2. Tests (no live calls)

Verification: `uv run python -m pytest -q tests/test_fusion_plan_bench.py` — 27 passed, 0 live calls.

- [x] 2.1 Add `tests/test_fusion_plan_bench.py`: structural score, panel family diversity, capacity spend guard, anonymisation, validator enforcement, cap enforcement, panel too small, receipt field allowlist, config.

## 3. Docs and ADR

Verification: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/fusion-plan-bench` exits 0; `uv run python scripts/check_doc_links.py` exits 0. Test: both scripts pass.

- [x] 3.1 Add `docs/adr/ADR-047-verdict-native-fusion-planning.md`.
- [x] 3.2 Add ADR-047 entry to `docs/adr/README.md`.
- [x] 3.3 Add and validate OpenSpec change `openspec/changes/fusion-plan-bench/`.

## 4. Gates (must all pass before push)

Verification: each command exits 0.

- [x] 4.1 `uv run ruff check .`
- [x] 4.2 `uv run ruff format --check .`
- [x] 4.3 `uv run mypy verdict --strict`
- [x] 4.4 `uv run python scripts/check_reachability.py --ratchet-base origin/main`
- [x] 4.5 `uv run python scripts/check_lint_gates.py`
- [x] 4.6 `uv run python -m pytest -q` (full suite)
- [x] 4.7 `npx --yes @fission-ai/openspec@1.13.2 validate fusion-plan-bench --strict --no-interactive`

## 5. Push and PR

Verification: `git ls-remote origin refs/heads/feat/fusion-plan-bench` shows the head SHA; PR is open on GitHub. Test: branch visible on remote and PR URL accessible.

- [x] 5.1 `git push origin HEAD:feat/fusion-plan-bench`
- [x] 5.2 Open PR; do not merge.

## 6. Post-merge task

- [ ] (post-merge) Run `verdict bench fusion-plan` on the operator setup from merged main; record evidence in `docs/proof/dod/fu1/`.
