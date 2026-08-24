# Operational Loop Validation Checkpoint

This is a recovery/planning-stage checkpoint. No implementation or full test run has been claimed. The recovery audit is reconciled and the user explicitly authorized implementation on 2026-08-24.

## Current state

- Worktree: `feat/verdict-operational-loop`
- Base commit: `762335eef314ffd7e7fff4c098e586533d2ca3d6`
- Current complete tracked/untracked status-list digest before implementation (`git status --porcelain=v1 -z --untracked-files=all`): `sha256:339a216b73d8b2d2f615a378d8bc520737af8412adcab4617b9fb07b0388d13d`
- Feature artifacts: `specs/272-operational-routing-loop/`
- OmniRoute task routing: live-observed disabled (`taskRouting.enabled=false`)
- OmniRoute detection: live-observed disabled (`detectionEnabled=false`)
- Live OmniRoute runtime: healthy `3.8.49`; no upgrade performed

## Read-only live runtime observations

Observed on `2026-08-24` using OmniRoute CLI/API reads only; no setting was
changed:

- Health: `healthy`; version `3.8.49`; 299 catalog providers, 40 configured, 26
  active, and 9 monitored.
- Circuit state: 0 open, 2 half-open, 0 degraded, 7 closed.
- The health summary reports 43 active connections. The `providers status`
  projection returned zero rows in this observation, so no per-connection
  readiness claim is made from that command.
- Model listing returns concrete catalog entries, including Claude subscription
  routes and alternative-provider models. The listing does not report usable
  context limits in this CLI projection and does not prove live protocol/tool
  compatibility.
- Aggregate 24-hour telemetry reports 11 requests, 4,082 ms mean latency, and zero
  errors for its current sample.
- `omniroute quota` returns `No quota data`; quota/headroom are therefore
  `UNKNOWN`, not zero and not available.

Proof classification: health/settings/provider/telemetry are `LIVE-PROVEN` for
the observation time; model suitability remains `PARTIAL` until the exact route
passes the Phase 1 protocol and work-unit qualification.

The live task-routing settings were previously observed and preserved as
`taskRouting.enabled=false` and `detectionEnabled=false`. This implementation did
not mutate them. Historical backups and catalog projections are not current
settings proof; T019 must re-observe the live endpoint before inference.

## Frozen Phase 1 work unit

- Unit: `headroom-unknown`
- Owned paths: `verdict/headroom.py`, `tests/test_headroom.py`
- Verification: `uv run pytest -q tests/test_headroom.py`
- Expected behavior: absent or unavailable provider headroom evidence remains
  unknown/unavailable and never becomes a fabricated `100%` capacity signal.
- Isolation: disposable Git worktree per attempt; replay only a verified patch.

## Evidence boundary

The handoff records a baseline of 1,449 passing tests after dependency sync. That is prior evidence, not a substitute for rerunning tests after implementation. Catalog/source/CI evidence must remain separate from live provider quota, public install, registry publication, release, and launch evidence.

## T010 cross-model continuation and drift refusal

Observed on `2026-08-24` from the active dirty snapshot, without changing the
frozen work unit:

- Source commit: `762335eef314ffd7e7fff4c098e586533d2ca3d6` on
  `feat/verdict-operational-loop`.
- Captured dirty snapshot digest:
  `sha256:0b553089f905020e8da18ac2fe984bda77b33bed73f3c0161d3c942689239dc2`.
- Packet: `/tmp/verdict-operational-loop-t010/headroom-unknown.json`, kept
  outside the repository so its creation did not change the captured snapshot.
- Packet integrity digest:
  `sha256:c912593ad7522d8ec34d48c9e5b009a6677b17a8229d879171d18c5bafe01f3c`.
- Packet file SHA-256:
  `08de2661c28cd534cb8b167b6c2ebeef7e48cd7d47ff9744677434a9bbab599a`.

The packet was exercised through the public CLI:

```text
uv run verdict autodev packet create --packet /tmp/verdict-operational-loop-t010/headroom-unknown.json --from /tmp/verdict-operational-loop-t010/headroom-unknown-source.json --json
uv run verdict autodev packet inspect --packet /tmp/verdict-operational-loop-t010/headroom-unknown.json --json
uv run verdict autodev packet validate --packet /tmp/verdict-operational-loop-t010/headroom-unknown.json --json
uv run verdict autodev packet resume --packet /tmp/verdict-operational-loop-t010/headroom-unknown.json --model cc/claude-fable-5 --json
```

`create`, `inspect`, and `validate` produced byte-identical canonical packet
JSON. `resume` preserved the packet integrity digest while adding only the
non-integrity executing-model handoff field.

A fresh Claude Code print session was invoked with `--model fable`, no session
persistence, and only one `Read` capability. It read only the resumed packet and
accurately reported the exact repository/branch/commit/dirty and lock digests,
goal/non-goals, acceptance, owned and denied paths, budgets, completed T001-T009,
active T010, remaining T011-T022, and the next safe action. The response metadata
did not expose a provider-resolved model ID; the only identity evidence is the
requested CLI alias `fable`, the packet label `cc/claude-fable-5`, and the model's
self-reported harness label `Fable 5`. This is cross-family live continuation
proof, not resolved-route identity proof. A first deliberately tool-free run
truthfully refused to invent the packet contents, demonstrating fail-closed
behavior when the packet itself is unreachable.

The continuation's next trusted read-only check, `git status --short`, completed
and reproduced the expected modified/untracked feature scope. No source file was
changed by the continuation model.

For drift proof, a valid canonical revision was created with only the authoritative
goal changed and its integrity digest recomputed. Calling
`original.validate_resume(revised)` deterministically returned:

```text
ExecutionPacketError: immutable packet drift; create a new packet version
```

The drifted packet SHA-256 is
`43d73df0823fbb297849ce5c1b9050b895a00ab8d383435c503e09064f551e72`.
Artifacts remain in `/tmp/verdict-operational-loop-t010/` for local inspection;
they are not release, registry, public-install, or Phase 1 live-patch proof.

Focused post-proof gates:

```text
uv run ruff check verdict/execution_packet.py verdict/cli.py tests/test_execution_packet.py tests/test_execution_packet_security.py tests/test_cli_inprocess.py
# All checks passed!
uv run mypy --strict verdict/execution_packet.py
# Success: no issues found in 1 source file
uv run pytest -q tests/test_execution_packet.py tests/test_execution_packet_security.py tests/test_cli_inprocess.py -k packet
# 28 passed, 45 deselected
```

## Pre-US1 code-impact evidence

The installed Code Review Graph was inspected before the T011-T018 change wave.
It contained 4,633 nodes, 37,029 edges, and 270 files, built on this branch at
the base commit `762335eef314`; it therefore describes the committed baseline
and does not yet include the untracked packet implementation. Graph queries
identified ten existing callers/tests of `run_autodev`, existing
`ContextPackCompiler` consumers, and three tests that directly assert the old
fail-open `check_headroom` behavior. Those tests are an intentional Phase 1
compatibility impact and must be reconciled only after the live model authors
the frozen patch. The graph is useful but stale for current dirty changes, so
its blast radius is `PARTIAL`; post-change analysis must refresh it before any
completeness claim.

## US1 red-test ledger

- T011: `uv run pytest -q tests/test_autodev_operational_routing.py`
  collected seven tests and produced seven intended failures because the thin
  `verdict.autodev_routing` adapter does not exist yet. The file passes Ruff and
  establishes RED for concrete-route filtering, capability/freshness evidence,
  requested/resolved/actual identity, explicit unknown quota/headroom,
  eligibility-before-ranking, and normalized retry safety.
- T012: `uv run pytest -q tests/test_autodev_context.py` collected three tests
  and produced three intended failures because the thin
  `compile_worker_context` composition seam does not exist yet. This establishes
  RED before T015; there were no syntax or collection errors.
- T013: `uv run pytest -q tests/test_autodev_operational_loop.py` stopped at
  the intended collection error because `run_packet_autodev` is not yet
  implemented. The new lifecycle test file passes Ruff and establishes RED for
  packet/source validation, pre-inference checkpointing, patch attribution,
  owned-path enforcement, external verification, clean attempt isolation, one
  fallback, resume idempotency, receipt redaction, and truthful failure.

## Next validation commands

```text
uv run pytest -q tests/test_operational_loop_contracts.py tests/test_operational_loop.py tests/test_operational_loop_explanations.py tests/test_operational_loop_recovery.py
ruff check .
mypy --strict verdict/
uv run pytest -q
```
