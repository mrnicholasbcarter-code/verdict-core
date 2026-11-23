# Tasks

## 1. Tests first

Write the failing test `test_repeated_ingest_supersedes_previous` in
`tests/test_context_lift.py` before the fix. Verify it fails with
`ContextIntelligenceError: contradiction_requires_explicit_supersession`.

- [x] 1.1 Add `test_repeated_ingest_supersedes_previous` to `tests/test_context_lift.py`

## 2. Implementation

Apply the fix to `verdict/context_lift.py::ingest_lift_fact`: look up the
active record and pass `supersedes` on the write; use accurate `ingest_refused`
code on non-secret ingest failure.

Verify: run `uv run python -m pytest -q tests/test_context_lift.py` — all eight
tests including the new one must pass.

- [x] 2.1 Add `existing = gate.plane.get("patterns", "lift_fact")` before the write in `ingest_lift_fact`
- [x] 2.2 Pass `supersedes=existing.record_id if existing is not None else None` to `MemoryWriteRequest`
- [x] 2.3 Change error code on non-secret ingest failure from `secret_refused` to `ingest_refused`

## 3. Full verification

Verify: run full suite `uv run python -m pytest -q -n auto -p no:cacheprovider`,
ruff, mypy, and `uv run python scripts/check_lint_gates.py`. All must pass.

- [x] 3.1 Full test suite green
- [x] 3.2 `uv run ruff check . && uv run ruff format --check .` clean
- [x] 3.3 `uv run mypy verdict --strict` clean

## 4. Live check

Verify: on installed main, the next daily context-lift run and acceptance run
show doctor green.

- [ ] (post-merge) 4.1 On installed main, the next daily context-lift run and the next acceptance run show doctor green
