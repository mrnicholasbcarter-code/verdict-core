# Issue 455 README Evidence

**Evidence state:** Prospective implementation evidence; human skim and final independent review remain incomplete.

## Source binding

- Base commit: `1a983b1a262aa6d5eb4213004eb39b852c5cb0a9`
- Tracked-diff SHA-256 immediately before the clean-install capture: `3fdc8b263cd3f716a325e12ff14cf3079a987c5a6a95bf715ebbdf7a11ddbb46`
- Latest tracked-diff SHA-256 after focused verification: `fab19c3b49d5c57a77d54f24b1b994affd3d426130e0a247666ecc7e45e9a10c`
- Untracked planning/evidence files are enumerated by the final `git status --short`; the unrelated pre-existing `handy.txt` is excluded from this feature and was not read or modified.
- Limitation: these hashes cover tracked diffs only. This remains a mutable dirty worktree containing untracked specification/evidence files, not an immutable snapshot, final commit, or final PR head. Final gates must be rebound after the intended files are committed.

## Fresh isolated installation and terminal capture

Observed on 2026-09-07 using an isolated directory under `/tmp` and a newly built wheel from this worktree.

- Build: `uv build --wheel --out-dir <temp>/wheel <worktree>`
- Environment: new `uv venv`; installed only from the generated `verdict_core-0.2.0-py3-none-any.whl`
- Execution environment: `env -i` with only isolated `HOME`, venv/system `PATH`, `PYTHONDONTWRITEBYTECODE=1`, and `NO_COLOR=1`
- Command: `verdict quickstart --non-interactive --dry-run`
- Exit status: `0`
- Empty execution-directory entries after the command: `0`
- Terminal capture: [`issue-455-quickstart.typescript`](issue-455-quickstart.typescript)
- Capture tool: util-linux `script` 2.42.2, invoked non-interactively with child-exit propagation

Observed output:

```text
Verdict credential-free quickstart
===================================
Task: Add structured output to the invoice parser
Required capabilities: structured_output, tools
Selected route: demo/frontier-tools
Excluded candidates: 3
Receipt: fixture:issue-35 (deterministic_fixture)
Status: PASS
- demo/no-tools: missing capability: tools
- demo/quota-empty: quota exhausted
- demo/unverified: health unknown
```

Limitations: the capture proves an isolated wheel installation and deterministic no-key execution. It does not prove live-provider behavior, production readiness, human comprehension, or final PR status.

## Focused regression evidence

Red phase after adding exact-output and link/fragment tests:

- `test_readme_quickstart_output_matches_packaged_report` failed because README omitted the canonical `Task:` and `Required capabilities:` lines.
- `test_readme_recruiter_path_is_ordered_and_local_links_resolve` initially failed because the validator treated a valid directory target (`docs/adr/`) as requiring a regular file.

Green phase after minimal corrections:

- Command: `.venv/bin/python -m pytest -q tests/test_flagship_demo.py::test_readme_quickstart_output_matches_packaged_report tests/test_documentation_smoke.py::test_readme_recruiter_path_is_ordered_and_local_links_resolve`
- Result: `2 passed`

Final focused checks on the implementation dirty worktree:

- `ruff check tests/test_flagship_demo.py tests/test_documentation_smoke.py verdict/flagship_demo.py`: pass (`All checks passed!`)
- `ruff format --check` on those three files: pass (`3 files already formatted`), with the repository's pre-existing split-on-trailing-comma configuration warning
- focused pytest: `19 passed in 7.34s`
- `bash -n quickstart.sh`: pass
- `git diff --check`: pass
- `mypy verdict --strict`: pass (`Success: no issues found in 136 source files`)
- wheel build: pass (`verdict_core-0.2.0-py3-none-any.whl`)
- proof-matrix validator: pass (`15 rows`, `11` claims-ledger entries)
- proof-matrix tests: `3 passed in 0.06s`

Full pytest did not pass: `1840 passed, 2 failed, 1 skipped, 1 warning in 316.49s`. Failures were outside this feature's files:

- `tests/test_governed_swarm_supervisor.py::test_envelope_link_and_narrowing_only_mutations`: expected `cannot broaden`, received `path '/tmp/escape' escapes allowed roots`.
- `tests/test_memory_adapters.py::test_paths_require_allowlisted_non_tmp_root_and_no_symlink`: expected a `ManifestError` mentioning `/tmp`, but no exception was raised.

These failures remain blockers unless authoritative CI or a separate investigation establishes them as environment-specific and the final reviewed head is green.

## Deterministic cost wording

Observed command: `.venv/bin/python -m verdict.routing_demo --mock`

Observed summary:

- status `completed`, mode `mock`
- requests `100`
- routed `$0.160000`
- baseline `$0.520000`
- savings `$0.360000 (69.2%)`

README wording describes these as approximate deterministic estimates and not observed invoices. This observation does not establish live quality, latency, availability, or production savings.

## Automated skim structure

Automated documentation checks establish only that:

- positioning precedes install;
- install precedes the no-key proof and exact output;
- the receipt precedes the `$0.16`/`$0.52` cost comparison;
- architecture follows the primary proof journey;
- one install command appears in the install section;
- changed local README paths and heading fragments resolve.

This is structural evidence, not evidence that a human understood the project within 90 seconds.

## Human 90-second skim

**Status: not-run.** A real reviewer must record whether they identified product, install, proof, and next action within 90 seconds. Automated checks and this author’s review do not satisfy that criterion.

## Requirement traceability

| Requirement | Evidence/status |
|---|---|
| FR-001–FR-002, SC-003 | Automated ordering assertions in `tests/test_documentation_smoke.py`; local pass must be reconfirmed in final suite |
| FR-003, FR-012, SC-001 | Isolated wheel install, empty environment, exit 0, and zero execution-directory entries |
| FR-004–FR-006, FR-011, SC-002 | Canonical renderer-to-README exact-output regression and shared CLI path |
| FR-007–FR-008, SC-007 | Observed mock summary plus README fixture/mock/live limitations |
| FR-009–FR-010, SC-005 | Metadata wording and local path/fragment regression |
| SC-004 | Pending final focused suite and lint record |
| SC-006 | Automated structure observed; human comprehension remains `not-run` |
| Terminal recording/GIF | Actual `.typescript` terminal capture exists; a GIF was not generated because no installed supported conversion tool was verified |
| Full required suite | Pending final local execution and exact-head CI recheck |
| Independent review | Pending; implementation author cannot supply independent review |
