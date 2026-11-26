# Tasks

## 1. Specification

Write the proposal, design and spec delta before touching production code. Verify:
`npx @fission-ai/openspec@1.13.2 validate meta-immutable --strict` and the vendored
validator both pass.

- [x] 1.1 Write proposal.md, design.md, tasks.md and the spec delta
- [x] 1.2 Both OpenSpec validators pass

## 2. Tests first

Write failing tests before the fix. Verify: each new test is red on the unmodified
code and the failure message names the unmet condition (not an import error or crash).

- [x] 2.1 `test_sources_field_is_read_only`: `snapshot.sources["__mutate_test__"] = None` raises `TypeError`
- [x] 2.2 `test_mapping_field_is_read_only`: `snapshot.mapping["__mutate_test__"] = None` raises `TypeError`
- [x] 2.3 `test_load_store_returns_same_cached_object`: two successive `load_store` calls on the same unmodified file return `is`-identical objects

## 3. Implementation

Apply the fix. Verify: run
`uv run python -m pytest -q tests/test_model_metadata.py` — all tests including
the new ones must pass.

- [x] 3.1 `store.py`: change `sources` annotation to `Mapping[str, SourceStatus]`
- [x] 3.2 `store.py`: change `mapping` annotation to `Mapping[str, Any]`
- [x] 3.3 `store.py`: add `MappingProxyType` import and `object.__setattr__` calls in `__post_init__`
- [x] 3.4 `extra.py`: change `"mapping": snapshot.mapping` to `"mapping": dict(snapshot.mapping)`

## 4. Gates

Verify: `uv run ruff check . && uv run ruff format --check .`, `uv run mypy verdict --strict`,
`uv run python scripts/check_lint_gates.py`, and `vgate` all pass.

- [x] 4.1 New tests pass, full suite shows no regression
- [x] 4.2 ruff check, ruff format, mypy --strict
- [x] 4.3 Doc links, reachability ratchet, lint gates, vgate

## 5. Live verification

Verify: on installed main, the inventory view still renders within the 1 s limit
(`test_real_inventory_to_view_and_render_under_1s` on the installed package).

- [ ] (post-merge) 5.1 Install merged main and confirm `verdict models` renders the route table within 1 s
