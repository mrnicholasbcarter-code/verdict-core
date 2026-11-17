# Tasks

## 1. Implementation

Verify with the focused tests in `tests/test_autodev_wiring.py` and `tests/test_decomposer.py`.

- [ ] 1.1 Add tests that fail while the planner default is a paid route.
- [ ] 1.2 Set `DEFAULT_ORCHESTRATOR_MODEL` to `cc/claude-sonnet-5-5`.
- [ ] 1.3 Keep an explicit `--orchestrator-model` as the winner.

## 2. Live check

Verify with a live proof on the operator setup after merge.

- [ ] (post-merge) Install from merged main and run verdict autodev --objective <small objective> --dry-run --json on the operator setup; record that planning used cc/claude-sonnet-5-5 and no HTTP 402.
