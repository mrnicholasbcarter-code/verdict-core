# Tasks

## 1. Selection path

- [x] 1.1 Add `prepare_live_autodev_selected` beside the unchanged pinned
      `prepare_live_autodev`; verify with
      `pytest tests/test_autodev_wiring.py -k selected`
- [x] 1.2 Order the ladder by rank, keep task-eligible FREE routes only, probe
      each with the pinned path's identity/content/zero-cost checks, and drop
      failures with their reason; verify with the ladder-order and probe-drop tests

## 2. Fail closed

- [x] 2.1 Raise when no eligible free route exists, before any probe or plan;
      verify with the fail-closed test (no probe call, no paid route named)
- [x] 2.2 Raise with every dropped route and reason when no probed route
      answered correctly; verify with the probe-drop test's second phase

## 3. Wiring and evidence

- [x] 3.1 `cmd_autodev` uses the selection path when no pin is given and reports
      `executor_selection`; verify with the in-process CLI test
- [x] 3.2 Record `execution_path_why_selected`, `execution_path_rejected` and
      `executor_probe_dropped` in the autodev outcome receipt; verify by reading
      the persisted receipt back
- [x] 3.4 Return the probe-dropped routes from the selection call and report them
      in `executor_selection.dropped`; verify with the probe-drop and CLI tests
- [x] 3.3 Run the focused and neighbour suites plus ruff and mypy --strict on the
      touched files as the proof gate
