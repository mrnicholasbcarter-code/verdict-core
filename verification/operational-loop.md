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

## T042 — US1 live exit demonstration (LIVE-PROVEN, 2026-08-24T18:02Z)

One command (`verdict.cli autodev packet execute --packet .verdict/packets/headroom-unknown-r8.json --repo . --allow-live --json`) performed a real bounded repository edit through a concrete non-primary route with independent verification. Terminal state `completed`, proof level `live-proven`.

| Field | Value |
|---|---|
| Source commit | `5695afec` (branch `feat/verdict-operational-loop`) |
| Packet | `.verdict/packets/headroom-unknown-r8.json` (`headroom-unknown-r8`, v2 over seed via `parent_integrity_digest`) |
| Requested route | `claude/claude-haiku-4-5-20251001` @ `omniroute-local` (`http://127.0.0.1:20128/v1`, protocol `openai.chat`) — concrete non-primary route; no `auto/*` resolver involved |
| Evidence digest | `sha256:7c4e2b5da19cffed7...` (fresh source-linked observation, ttl 300 s, quota/headroom explicitly UNKNOWN) |
| Changed paths | `verdict/headroom.py`, `tests/test_headroom.py` (both inside owned set) |
| Artifact digest | `sha256:6783ce91cdcfe73c34722ff856028e8c46e554ff3cf353aaddaa3c0b6fc602d7` — recomputed from replayed tree matches receipt exactly |
| Verification | trusted argv (`.venv/bin/pytest -q tests/test_headroom.py`) run in isolated attempt worktree outside the worker: exit 0, 2 passed; re-run green in target tree post-replay |
| Checkpoints | `before_inference` = `rcpt-4e7fa65888bb4ad4be04d111e7423d6f`; terminal = `rcpt-7667f360f13740efa8b4adc1463f69f5` |
| Usage / quota | token counts observed but REDACTED in receipt per redaction authority; quota/headroom UNKNOWN (gateway exposes none) |
| Fallbacks used | 0 of max 1 |

Patch substance: `check_headroom` returns `None` (unknown) instead of fabricating `100.0` when no headroom endpoint exists, plus new tests asserting the unknown case.

### Defects found and fixed by the live demonstration

1. **Corrupt model patches rejected wholesale** (`5f8b804`): both gemini-2.5-flash and claude-haiku emitted miscounted hunk headers / phantom EOF context; `extract_diff` now recounts hunks from actual bodies before `git apply`.
2. **Verification tested the wrong tree** (`a323473`): the venv editable-install `.pth` pinned `verdict` to the main checkout, so the isolated attempt worktree silently verified unpatched code. Fixed with `PYTHONPATH=<attempt_repo>` on the verification subprocess.
3. **Untracked attempt files dropped on replay** (`5695afe`): `git diff HEAD` excludes new files, so a worker-created test file was verified in scope then lost on replay. Replay now copies untracked attempt files.
4. **Clock-skew freshness crash**: gateway observations fractionally in the future raised `freshness_seconds must be non-negative`; clamped to 0 (earlier commit `660c854`).

Limitations: usage token values are redacted at the receipt layer so cost is recorded as present-but-unreadable here; quota/headroom remain UNKNOWN because the gateway does not expose them; attempt latency recorded (~56 ms executor overhead) excludes full inference wall-clock.

Post-artifact full suite: 1,511 passed, 2 failed — both failures
(`tests/test_golden_path.py::test_timeout_is_bounded_and_denies`,
`::test_changed_path_outside_declared_boundary_denies`) reproduce
identically at merge-base `762335ee` and are pre-existing on main,
unrelated to this branch. The three headroom tests asserting the old
fail-open contract were reconciled in `bb7c7c1`.

## T043 — Forced single-fallback demonstration (LIVE-PROVEN, 2026-08-24)

Driver: direct `run_packet_autodev` invocation (CLI does not wire
`refresh_fallback`); packet `headroom-fallback-t043b`, source commit
`d816d1e`'s parent tree state captured via `capture_source_binding` at run time.

Scenario per frozen Demonstration C: the primary route's `base_url` pointed at a
closed local port (`http://127.0.0.1:9/v1`) — a safe, fully reversible induction
touching no repository path. The failure was classified `worker_failed`
(transport error, connection refused). `refresh_fallback` then re-observed LIVE
gateway evidence for the fallback route; the evidence digest refreshed from the
pre-run observation.

| Field | Value |
|---|---|
| Packet | `.verdict/packets/headroom-fallback-t043b.json` (v2 over seed) |
| Work unit | open micro-unit: explicit `__all__` in `verdict/headroom.py`; trusted gate proven RED pre-inference |
| Attempt 1 | requested/actual `cc/claude-haiku-4-5-20251001` @ unreachable endpoint → classified `worker_failed` |
| Fallback refresh | fresh live evidence digest `sha256:2a55d45489aef3802...` observed after classification |
| Attempt 2 | same cc/* subscription route against live gateway → applied, verified=True |
| Receipt chain | `before_inference rcpt-1660fe6e` → attempt 1 (`worker_failed`, unverified) → attempt 2 (verified) → terminal `completed rcpt-22acdc7b` |
| Clean scope | attempt 1 ran in its own disposable worktree and was removed before attempt 2; no first-attempt change leaked (attempt 1 produced zero file changes) |
| Terminal bound | loop bound `index < 2` + fallback only permitted at index 0; admission check additionally refuses any non-`cc/`-prefixed fallback route (verified True) |
| Verification | trusted argv exit 0 in attempt scope; independent post-replay `pytest -q tests/test_headroom.py`: 2 passed |
| Artifact | committed as `d816d1e` (`__all__ = ['check_headroom']` + dangling comment removed) |
| Quota/headroom | UNKNOWN (gateway exposes none); usage redacted at receipt layer |

Exactly one fallback appears in the receipt chain (AC-1.8, AC-1.13). Focused
gates post-artifact: 18 passed (headroom + public helpers + omniroute edge
cases), 15 passed (`test_autodev_operational_loop.py`).

## T044 — Phase D promotion gate

### Gates from final source state (`00519b1`, 2026-08-24)

| Gate | Result |
|---|---|
| Full suite | 1,511 passed, 2 failed — both failures are `test_golden_path.py::test_timeout_is_bounded_and_denies` and `::test_changed_path_outside_declared_boundary_denies`, proven pre-existing at merge-base `762335ee` |
| Lint (`ruff check .`) | All checks passed |
| Strict type-check (`mypy --strict verdict/`) | Success: no issues in 116 source files |
