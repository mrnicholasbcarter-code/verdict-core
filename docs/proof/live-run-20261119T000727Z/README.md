# Live orchestrate run 20261119T000727Z (evidence record)

**LIVE, not a fixture.** One real `verdict orchestrate` run against the
operator's local OmniRoute gateway, recorded 2026-11-19 from clean `main`
`d5e88ee801577ac2f55ea564c3b49734eacde732` (Verdict 0.5.0). Every model call
was live.

**Raw event log retained privately; digest recorded here.** `events.jsonl` and
`receipt.json` are digest-bound (the receipt pins the SHA-256 of
`events.jsonl`), so they can only be committed byte for byte. Both contain
absolute host paths (the interpreter path of the producer checkout and the
temporary run directory). The repository has no supported export or
normalisation step for digest-bound bundles, the push gate rejects home
paths under `docs/proof/`, and [REDACTION_POLICY.md](../REDACTION_POLICY.md)
says private values are replaced by a digest. So this directory is an evidence
record, not a verifiable bundle: `verdict run-receipt` cannot be run on it.
The operator ran it on the private run directory; its output is quoted below.

## Run

| Item | Value |
| --- | --- |
| Run id | `20261119T000727Z` |
| Source SHA (producer, clean tree) | `d5e88ee801577ac2f55ea564c3b49734eacde732` |
| Goal | Add and test a pure integer subtract function |
| Window | 2026-11-19T00:07:30Z to 00:10:53Z (run 203 s) |
| Outcome | `COMPLETE`: all implement/integrate nodes validated, barrier ok, review PASS |
| Worker | node `subtract`, 1 attempt, route `antigravity/gemini-3-flash` (capacity class `free`), served identity verified by the gateway, 2579 input / 147 output tokens, 1.13 s |
| Reviewer | open-code-review v1.12.9 on route `kilocode/cohere/north-mini-code:free`, status PASS, 0 blocking, 64.9 s; implementer family `gemini` excluded (family-level independence) |
| Integration commit | `136c396b19ce9e9909df0d28544866160bb9c473` (2 new files, 19 insertions; see [code.diff](code.diff)) |
| Events digest | `sha256:0738e67952854c8522334b5f50cb6e941645acd4b6562e1fdc93f8254b4a0593` (28 events) |
| Graph digest | `sha256:03e50037cfb8665de6383c810b5b46ee15011f7b6d2a248a82d41813b202d70b` |

Command (paths neutralised):

```bash
verdict orchestrate --graph graph.json --repo <worktree of the SHA> --runs-dir <runs> \
  --state-file <isolated>/orchestration-health.json --gateway http://127.0.0.1:20128 \
  --capacity free,subscription --executor direct-gateway --max-parallel 1 \
  --max-attempts-per-node 3 --attempt-timeout 180 --run-deadline 600 --plain
```

`verdict run-receipt` on the private run directory, right after the run:

```text
COMPLETE: all implement/integrate nodes validated, barrier ok, review PASS
integrity: OK (events digest verified)
  subtract           VALIDATED        antigravity/gemini-3-flash[success]
  review: PASS by open-code-review v1.12.9 on kilocode/cohere/north-mini-code:free
```

## Private files (sha256)

| File in the run directory | sha256 |
| --- | --- |
| `events.jsonl` | `0738e67952854c8522334b5f50cb6e941645acd4b6562e1fdc93f8254b4a0593` |
| `receipt.json` | `2293ba84576379e33fa8a1db69a4e515cfadd0b3ad371bff1223339250ee50ab` |
| `graph.json` | `bf2da8956f77ffce05f37bddd7928e62e24ba3d1b9d62417a539e21b9aa7e880` |
| `progress.json` | `0e758a1ede897985f7b217c9acbee55414cda5d66a3390d158d3756ac2e0aecb` |
| `outcome-records.jsonl` | `e10a22ccc618f2696c86441395a5a88324d22d864a561fff37f0cba37a2f90fc` |
| `attempts/subtract-a1.json` | `1aa93631ddcf96dec5911b47ead12c101200548bc311308f0c95dbb27c0258c8` |
| `context_artifacts/subtract.json` | `6d85a2e3b044cd1190c8521096032892b3640a0cef98502d3866ab6dc086bbcc` |
| `review/ocr-raw.json` | `fef17eb02abbbe2db84319b550f8588b6dc88291a4a7adaeb748758e49d3d602` |
| `review/verdict-ocr-changed-test-include.json` | `ce3508a276ab705c31361f059d0522c7b1e1df0674ee1a4af28d488439cd156e` |

## Files here

- [code.diff](code.diff): the integration commit's diff, byte for byte
  (sha256 `6503465b456622ccc65ea277abd888fa427cef17ddc570d40c6abd30def7fe0a`).
- [focused-test.txt](focused-test.txt): the focused test run in a fresh
  checkout of the integration commit. Sanitised copy: the checkout path was
  replaced with `<verify-worktree>` on two lines; nothing else changed. The
  original is not digest-bound (original sha256
  `9d90697dc2522d6d9bdd8fa0e6a97d2c11ceab727ffc775ed6495e211db452d4`).

## What VERIFY "4 passed, 0 failed" counts

The live view's VERIFY panel counts every passed check of three kinds: node
verifications, barriers and integrations (`verdict/orchestration/tui.py`).
In this run that is 4 events:

1. `verify`: node `subtract` ran `pytest tests/test_interview_subtract.py`,
   exit 0, **3 passed** (the test file has 3 tests).
2. `barrier` `ownership`: 2 file(s) within ownership.
3. `integrate`: 1 commit integrated.
4. `barrier` `integration`: 1 validated commit merged.

The three `repo_gate` events (`ruff check .`, `ruff format --check .`,
`mypy --strict verdict`, all exit 0) are recorded in the receipt but are not
part of that count.

## Limits

- The run used existing route-health state: a private copy of the operator's
  health cache, proof passports, free-model set and cooldown state taken at
  00:06Z. With an empty state directory no free route was eligible. This is
  not a cold-start run.
- The reviewer made one comment call with two non-blocking comments (one
  medium, one low). The call had malformed JSON and the review tool dropped
  it, so 0 findings were recorded (tracked in GitHub issue #1137). Only
  critical and high findings block in Verdict, so the status would be PASS
  either way.
- One run. No speed, cost or savings claim is made from it.

## Demo recording

[`docs/assets/live-run.svg`](../../assets/live-run.svg) (animated),
[`live-run.gif`](../../assets/live-run.gif) and the asciicast
[`live-run.cast`](../../assets/live-run.cast) replay this run's terminal
capture at 86 x 44. Edits from the raw capture: identical redraws dropped
(59 of 1581 kept), each pause capped at 1.5 s (62 s total), host paths
replaced with `.venv/bin/python` and `runs/`, a labelled command line added
before the run, and the separately captured `verdict run-receipt` output
above added after it. The generator reads the private capture, so it is not
committed.
