# Live orchestrate run 20261120T113919Z (evidence record)

**LIVE, not a fixture.** One real `verdict orchestrate` run against the
operator's local OmniRoute gateway, recorded 2026-11-20 from clean producer
checkout `2c85357ffe339e4363c018d4576b99a501520d8d` (Verdict 1.0.0). Every
model call was live.

**Raw event log retained privately; digest recorded here.** `events.jsonl` and
`receipt.json` are digest-bound (the receipt pins the SHA-256 of
`events.jsonl`), so they can only be committed byte for byte. Both contain
absolute host paths (the worktree path in `producer.repo` and the run
directory). The repository has no supported export or normalisation step for
digest-bound bundles, the push gate rejects home paths under `docs/proof/`, and
[REDACTION_POLICY.md](../REDACTION_POLICY.md) says private values are replaced
by a digest. So this directory is an evidence record, not a fully verifiable
bundle: `verdict run-receipt` must be run on the private run directory, not on
this directory. The operator ran it on the private run directory; its output is
quoted below.

## Run

| Item | Value |
| --- | --- |
| Run id | `20261120T113919Z` |
| Producer SHA (clean tree) | `2c85357ffe339e4363c018d4576b99a501520d8d` |
| Goal | Fix `verdict doctor`: wire a real gateway probe into `doctor_capability_report` so `gateway.inventory` and `gateway.execute` report covered when OmniRoute is healthy. Files: `verdict/capability_bootstrap.py`, `verdict/doctor_diagnostics.py`, new test `tests/test_doctor_capability_gateway_probe.py`, OpenSpec change `doctor-capability-gateway-probe`. |
| Window | 2026-11-20T11:39:31Z to 2026-11-20T11:48:00Z (509 s) |
| Outcome | `COMPLETE`: all implement/integrate nodes validated, barrier ok, review PASS |
| Planner | 1 attempt, `claude/claude-opus-5-5` (subscription) |
| bootstrap_fix | 1 attempt, `claude/claude-opus-5-5` (subscription), 33 972 input / 808 output tokens, 10.2 s |
| diagnostics_fix | 2 attempts, `cc/claude-opus-5-5` (subscription): attempt 1 `failure:verification_failed`, attempt 2 success; 47 352 total input / 13 722 total output tokens, 111.9 s total |
| probe_spec | 1 attempt, `claude/claude-opus-5-5` (subscription), 9 749 input / 3 097 output tokens, 24.9 s |
| integrate | merge success |
| Reviewer | REVIEW_INDEPENDENCE tried family-level exclusion (`claude`); no eligible reviewer from a different family available; fell back to route-level exclusion (`cc/claude-opus-5-5` and `claude/claude-opus-5-5` excluded), selected `cc/claude-sonnet-5-5`; open-code-review v1.12.9, status PASS, 0 blocking comments, 24.5 s, 85 457 tokens |
| Integration commit | `1dd58fe2b10d2b656b92bfb7465a588d3de6bdea` (run-internal merge; PR squash `a4acddf37d74fb06a782006dd83751ae63f9cf4e`) |
| PR | [#1191](https://github.com/mrnicholasbcarter-code/verdict-core/pull/1191), 32 checks pass, in-run reviewer Sonnet 5.5 (0 comments) |
| Events digest | `sha256:6c4ddd8051a9443793ceb8d7d186a1654f096d665bd3be15156244ae8557cab6` (101 events) |
| Graph digest (receipt) | `sha256:a83562002d1e069230ce052cc4a2e7c0c0ba58db41f392cccfc8b7b9de7409ed` |

Command (paths neutralised):

```bash
verdict orchestrate --graph graph.json --repo <worktree of real-story-16> --runs-dir <runs> \
  --state-file <isolated>/orchestration-health.json --gateway http://127.0.0.1:20128 \
  --capacity subscription --executor direct-gateway --max-parallel 1 \
  --max-attempts-per-node 5 --attempt-timeout 300 --run-deadline 900 --plain
```

`verdict run-receipt` on the private run directory (Verdict at `a4acddf3`, which
is on origin/main as of 2026-11-20):

```text
COMPLETE: all implement/integrate nodes validated, barrier ok, review PASS
integrity: OK (events digest verified)
  bootstrap_fix      VALIDATED        claude/claude-opus-5-5[success]
  diagnostics_fix    VALIDATED        cc/claude-opus-5-5[failure:verification_failed] -> cc/claude-opus-5-5[success]
  probe_spec         VALIDATED        claude/claude-opus-5-5[success]
  integrate          VALIDATED        merge[success]
  review: PASS by open-code-review v1.12.9 on cc/claude-sonnet-5-5
```

Note: running `verdict run-receipt` with Verdict before `2c85357f` (#1190) fails
with `corrupt event: unknown event type 'lint_autofix'`. The `lint_autofix` event
type was added in `2c85357f`; `a4acddf3` (origin/main) includes it.

## Private files (sha256)

| File | sha256 | Notes |
| --- | --- | --- |
| `events.jsonl` | `6c4ddd8051a9443793ceb8d7d186a1654f096d665bd3be15156244ae8557cab6` | 101 events, 1.9 MB; contains host paths; digest-bound |
| `receipt.json` | `a04be858c71d45a60a64402366cea770f4e560654c0ca972f3d90d182a5907a6` | 1.9 MB; contains host paths in `producer.repo`; digest-bound (mode 600) |

## Files here

- [graph.json](graph.json): the input graph (25 KB). The receipt records its
  digest as `sha256:a83562002d1e069230ce052cc4a2e7c0c0ba58db41f392cccfc8b7b9de7409ed`.
  The file committed here sha256 `8344cbdc8dc07fc3888839394918993e104329aaf72910f284abcb686862811d`;
  the mismatch means the graph file on disk was updated after the receipt was
  sealed (the run writes additional state into it during execution). Neither
  copy is digest-bound.
- `outcome-records.jsonl`: per-node outcome sidecar (not digest-bound by the
  run receipt). Not committed: `.gitignore` excludes `*.jsonl`. Available in
  the private run directory. Contents: chosen models, token counts, latency,
  verification commands (`verified_commands`), repair counts. All commands use
  repository-relative paths; no host paths.
- [review/ocr-raw.json](review/ocr-raw.json): the raw OCR review result from
  open-code-review v1.12.9 on `cc/claude-sonnet-5-5`. Status COMPLETE, 0
  findings across 4 items, 85 457 tokens, 5 tool-call failures (all
  `code_search` exit-129 from `--max-count`; did not block review).
- [review/verdict-ocr-changed-test-include.json](review/verdict-ocr-changed-test-include.json):
  the changed-test include list passed to OCR.
- [code.diff](code.diff): the full diff for PR #1191
  (`a4acddf37d74fb06a782006dd83751ae63f9cf4e`). 8 files, 324 additions, 2
  deletions. No host paths. Contents: OpenSpec change (5 files), new test
  `tests/test_doctor_capability_gateway_probe.py` (182 lines), and diffs for
  `verdict/capability_bootstrap.py` and `verdict/doctor_diagnostics.py`.

## What REVIEW_INDEPENDENCE "fell back to route-level" means

The runtime first tried to exclude the entire `claude` model family from
reviewer selection. At the time of review, no route from a different family was
available, so the selector fell back to route-level independence: it excluded
the two implementer routes (`cc/claude-opus-5-5` and `claude/claude-opus-5-5`)
but allowed other routes in the same family. The reviewer `cc/claude-sonnet-5-5`
is a different model (Sonnet vs. Opus) and a different route, but the same
provider family as the implementers. The in-run review PASS result therefore
carries weaker independence than the previous live run (which used a different
family). See [AGENTS.md](../../../AGENTS.md): "Review runs on a separate route:
implementer routes are excluded, another model family is preferred."

## Limits

- This is one run, attempt 16 of a story whose goal and constraints were
  operator-tuned over attempts 7 through 15. The goal text included a full
  OpenSpec skeleton to guide `probe_spec`.
- The reviewer was from the same model family as the implementers (see above).
- `diagnostics_fix` required two attempts; the first failed
  `verification_failed`. The `lint_autofix` events (4 total) show ruff was
  applied before each gate check; no content change resulted from them.
- The run used the real ratings store.
- No speed or savings claim.
