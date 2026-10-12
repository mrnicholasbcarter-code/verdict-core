# Tasks

## 1. Core salvage implementation on the production retry path

Owned files: `verdict/orchestration/runtime.py`, `verdict/orchestration/salvage.py`,
`tests/test_orch_salvage.py`. Verification:
`/home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_orch_salvage.py`
from the worktree root. Dependencies: existing runtime contracts only; no installs.

- [ ] 1.1 Add `verdict/orchestration/salvage.py`: owned-path capture from a failed
  attempt worktree, credential screening before staging, digest/byte evidence,
  restore into a fresh worktree.
- [ ] 1.2 Wire `DagRuntime`: capture before the failed worktree is removed on the
  failure path; restore into the SAME node's next attempt worktree; exclude
  fault-injected attempts; keep no-change retries byte-identical to today.
- [ ] 1.3 Emit the bounded content-free `salvage` event and history record; paths,
  counts, digests and rejection reasons only.
- [ ] 1.4 Add focused tests that exercise the actual `DagRuntime`
  failure-then-retry restoration plus helper-level screening and ownership
  rejection tests; verify acceptance, verification and admission behavior is
  unchanged.

## 2. OpenSpec change and golden-path documentation

Depends on group 1 semantics (names and boundaries must match the runtime).
Owned files: this change directory, `docs/guides/orchestration-golden-path.md`,
`tests/test_bod308_docs.py`. Verification:
`/home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_bod308_docs.py`
and `/home/nick/dev/verdict-core/.venv/bin/python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/bod-308-partial-salvage`.

- [ ] 2.1 Write proposal/design/tasks/spec delta in verdict-change-v1 format and
  pass the vendored validator.
- [ ] 2.2 Document salvage preservation, retry restoration semantics, content-free
  receipt events and rejection boundaries in the golden-path guide.
- [ ] 2.3 Add `tests/test_bod308_docs.py` proving artifact presence, schema
  conformance and guide consistency.

## 3. Integration

Depends on groups 1 and 2. Verification: focused tests from both groups on the
integrated head
(`/home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_orch_salvage.py tests/test_bod308_docs.py`),
plus scoped ruff/mypy and doc-link checks as declared by the integrator. No
push, no merge to main, no dependency, provider, credential or global registry
edits.

- [ ] 3.1 Combine core and docs branches; confirm focused tests pass on the
  integrated head.
- [ ] 3.2 Independent review from a different model family reads the diff.
- [ ] 3.3 Report completion with evidence or a named blocker; no stubs, no
  competing runtime.
