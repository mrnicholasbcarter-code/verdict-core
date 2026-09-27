# the CLI migration to shared human-output helpers CLI migration plan

Goal: every `verdict` command renders its human output with Verdict's terminal design
(`verdict.terminal_ui.TerminalUI`). `--json` output, exit codes and side-effect/consent behaviour stay
byte-identical. `verdict/cli.py` shrinks to a thin parser + dispatch shim.

## Guard rails (non-negotiable)

- `tests/test_cli_golden.py`: 39 offline `--json` contracts. Must stay green after every unit.
- Existing CLI tests (`tests/test_cli.py`, `tests/test_cli_inprocess.py`, harness/metadata/runtime CLI tests)
  must stay green. Tests monkeypatch `verdict.cli.<name>`: `_omniroute_api_request`, `_build_route_gate`,
  `execute_offload_chat`, `cmd_autodev_packet_execute`, `_read_omniroute_token`, `detect_all_providers`,
  `console`, `cmd_autodev`. Moved handlers must look these up through the `verdict.cli` module at call time
  (or `cli.py` re-exports them), so the patches keep working.
- One family per unit; each unit owns its `verdict/commands/<family>.py` plus the matching slice of `cli.py`.
  Units run SEQUENTIALLY on `cli.py` (shared file), in parallel only when they touch disjoint files.

## Families and order

1. release/compat + models/catalog (smallest, pure JSON)
2. routing (route/run/compare/choose/simulate/receipt/replay/stats/suggest/cost-report)
3. runtime/prove-at-rest/metadata
4. setup/doctor/quickstart/plan (already TerminalUI-based; mostly extraction)
5. harness (7 targets)
6. memory/hook/mcp/uninstall
7. autodev/packet/resume

## Presentation contract (human mode)

- `ui.header(<command title>)` once; `ui.section(...)` per block; `ui.status(label, state, detail)` for
  facts; tables via Rich Table with TOKENS; errors via `ui.status(..., "failed", reason)` + nonzero exit.
- Plain / NO_COLOR / non-TTY: TerminalUI already degrades; never emit ANSI in plain mode.
- `--json` path never touches TerminalUI.
