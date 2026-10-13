# Tasks

## 1. Recall gated facts into worker packs

Owned files: `verdict/orchestration/worker_context.py`, `verdict/context_sources.py`, `verdict/memory_plane.py`, `tests/test_orch_worker_context.py`. Verification: `uv run python -m pytest -q tests/test_orch_worker_context.py tests/test_orch_planner.py tests/test_orch_selected_context.py tests/test_worker_context_precision.py tests/test_context_intelligence.py tests/test_context_intelligence_fabric.py`.

- [x] 1.1 Add optional namespace, sensitivity and verified-only hints to `NativeMemoryProvider`.
- [x] 1.2 Add `memory.search` last in worker packs with a node-scoped query and cited, redacted memory units.
- [x] 1.3 Report a missing or unreadable plane as a named omission and never create or write a plane.
- [x] 1.4 Open the plane with `MemoryPlane.open_readonly`, and redact every rendered record field (review findings).
- [x] 1.5 Add regressions for recall, exclusions, citations, ordering, redaction of content and metadata, unchanged database bytes, and missing or broken planes.

## 2. Validate the slice

Owned artifacts: this change directory. Verify with scoped `ruff check`, `ruff format --check`, `mypy --strict`, `python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/bod-306-worker-memory-recall` and `python scripts/check_reachability.py --skip-vulture --json`.

- [x] 2.1 Run the scoped linters, strict mypy, OpenSpec validation and reachability.

## 3. Named later increments (not implemented here)

Owner: BOD-306 and BOD-336. Verification: focused tests per increment plus a measured `context_lift` or review-round comparison.

- [ ] 3.1 Ingest reviewed session decisions, review findings, CI results and worker outcomes through MemoryGate.
- [ ] 3.2 Recall from the shared provider (BOD-336) through the same advisory gates.
- [ ] 3.3 Measure recall with `context_lift` and review-round counts.
