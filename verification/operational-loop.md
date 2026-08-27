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
| Terminal bound | loop bound `index < 2` + fallback only permitted at index 0; at this SHA fallback admission also required a `cc/`/`cx/` prefix (historical). HEAD `326d6ae`+ replaces that with the `primary` evidence role |
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

Those mechanical gates are **stale**. They bound `00519b1` on 2026-08-24 and did
not include an independent adversarial review. Closeout evidence below replaces
them as the current T044 record.

## Closeout 2026-08-27 — remaining LIVE-PROVEN binding

Implementation commit: `daf55e8` on `feat/verdict-operational-loop`
(`feat(272): record use-time route identity and freeze worker context`).
Parent: `0d7a22b46eee5cd8f80ebdadadff0457195229dd`. Dirty snapshot digest of the
closeout working tree immediately before that commit
(`git status --porcelain=v1 -z --untracked-files=all`):
`sha256:1cd10be98f05d66da1cad527c27c4981b75b0c8264a7f5348f3bb22f3f73c097`.
Adaptive-state snapshot deletions were restored and were not part of this
digest or any commit.

### Live gateway observation (do not mutate settings)

Recorded 2026-08-27T02:53–03:10Z. Task-routing and detection were **not**
changed by this closeout.

| Field | Observation | Proof |
|---|---|---|
| OmniRoute version | `3.8.49` via MCP `omniroute_get_health` | LIVE-PROVEN |
| `taskRouting.enabled` | `false` (parsed from `GET /api/settings` `taskRouting` JSON string, first successful read) | LIVE-PROVEN |
| `taskRouting.detectionEnabled` | `true` on that same read (historical T003 recorded `false`) | LIVE-PROVEN, **concern** |
| Quota CLI | `omniroute quota` → `No quota information available.` | LIVE-PROVEN UNKNOWN |
| HTTP `GET /v1/models` | Intermittent: 200 in 17 ms, later 10–30 s timeouts with 0 bytes | LIVE-PROVEN |
| Adapter `observe()` | `OpenAICompatibleEvidenceAdapter` over `OmniRouteAvailabilityAdapter` returned 0 candidates; report errors `('catalog transport: timeout',)`; no quota/headroom fabricated | LIVE-PROVEN fail-closed |
| Chat completions | `POST http://127.0.0.1:20128/v1/chat/completions` reachable | LIVE-PROVEN |

### T032 — live evidence adapter (LIVE-PROVEN with limitation)

The thin adapter in `verdict/autodev_routing.py` composes the existing
availability surface and leaves omitted optional facets `None` (unknown).
Focused fixtures pass. Live `observe()` against the HTTP catalog timed out and
returned an empty candidate set rather than inventing quota or headroom.
`omniroute quota` independently reports no quota data, so quota/headroom remain
`UNKNOWN`. Limitation: a populated live candidate list could not be collected
in this window because `GET /v1/models` was unreliable.

### T033 — requested vs actual identity (LIVE-PROVEN)

Use-time observation via `PatchExecutor` + `RouteObservation` against the live
gateway, disposable git repo, no worktree mutation:

| Field | Value |
|---|---|
| Requested alias | `kimi-coding/kimi-k2.5` |
| Actual served identity | `kimi-k2.5` (`body.model`) |
| Observation outcome | `ok` (HTTP 200) |
| `identity_mismatch` | `true` (fields kept distinct) |
| Attempt outcome | `rejected` (non-diff content; route still `ok`) |
| Quota/headroom | UNKNOWN (not present on the response; CLI has no quota data) |

Catalog vs runtime conflicts are retained on `CandidateEvidence.conflicts` with
per-field freshness (fixture-proven; no live catalog/runtime conflict payload
was available because catalog GET timed out).

### T035 — deterministic worker context (LIVE-PROVEN composition + SOURCE identity)

`compile_worker_context` now freezes plan/unit/pack timestamps so two runs on
the same inputs are byte-identical. Digest
`sha256:f5bc5745017b8df95287ecd4450ba667e62ae2a64fb4f143a0ef508dbfc38889`
reproduced twice (242 tokens / 4096). T042 already compiled this seam on a live
execute (`5695afec`).

### T036–T038, T040 — live packet loop (LIVE-PROVEN via T042/T043, re-verified)

These paths did not change in substance. Existing live demonstrations remain
valid under AC-P.4 (proof binds to source identity and trusted verification,
not to the executing model):

| Task | Live evidence | Current SHA regression |
|---|---|---|
| T036 drift abort before inference | T042 packet/source binding; fixture `test_autodev_operational_loop.py` | 16 passed |
| T037 isolated execute + owned-path reject | T042 real patch; T043 clean attempt worktrees | 16 passed |
| T038 trusted verification outside worker | T042 `.venv/bin/pytest -q tests/test_headroom.py` exit 0, 2 passed | `tests/test_headroom.py` 2 passed |
| T040 one fallback then terminal bound | T043 exactly one fallback, second refused | 16 passed |

### T044 — Phase D gates from this closeout source (LIVE-PROVEN mechanical)

Required closeout commands:

```text
uv run pytest -q tests/test_autodev_operational_routing.py tests/test_autodev_operational_loop.py tests/test_autodev_context.py tests/test_headroom.py tests/test_execution_packet.py tests/test_execution_packet_security.py
# 65 passed (later 66 after context identity test)
uv run ruff check verdict/autodev_run.py verdict/autodev_routing.py verdict/eligibility.py verdict/patch_executor.py verdict/cli.py
# All checks passed
uv run mypy --strict verdict/autodev_run.py verdict/autodev_routing.py verdict/eligibility.py verdict/patch_executor.py
# Success: no issues found in 4 source files
```

Full promotion gates after the eligibility identity fix:

| Gate | Result |
|---|---|
| Full suite (`uv run pytest -q`) | 1537 passed, 1 warning (Starlette TestClient deprecation). The two `test_golden_path.py` failures recorded at `00519b1` no longer reproduce. |
| Lint (`uv run ruff check .`) | All checks passed |
| Strict type-check (`uv run mypy --strict verdict/`) | Success: no issues in 116 source files |

Independent adversarial review of HEAD `0d7a22b`:
`/home/nick/dev/specs/272-operational-routing-loop/.sdd/t044-review.md` (FAIL).
Re-review of HEAD `326d6ae`:
`/home/nick/dev/specs/272-operational-routing-loop/.sdd/t044-rereview.md`
(grok-4.5; HIGH-1/2/3 reconciled; residual HIGH was unwired `primary` fallback).

### T044 mechanical gates at `326d6ae` (controller-verified 2026-08-27)

| Gate | Result |
|---|---|
| Full suite `uv run pytest -q` | **1541 passed**, 1 Starlette deprecation warning |
| `uv run ruff check .` | All checks passed |
| `uv run mypy --strict verdict/` | Success, 116 source files |

Operator (not an agent during T042/T043) later set
`taskRouting.enabled=false` and `detectionEnabled=false` via
`PUT /api/settings/task-routing`.

### Limitations

- Live HTTP catalog listing was flaky; T032 live candidate enumeration is
  fail-closed empty, not a populated live evidence set.
- Closeout observed `taskRouting.detectionEnabled=true` while `enabled=false`.
  Operator restored both to `false` after closeout. That is out-of-band, not
  an in-demo agent mutation.
- MCP `omniroute_check_quota` returns `percentRemaining: 100` with
  `quotaTotal: null`. That is **not** copied into Verdict evidence; CLI and the
  adapter treat omitted quota as UNKNOWN.
- T035–T038/T040 do not repeat a second live repository-mutating execute in this
  closeout; they bind T042/T043 live artifacts plus current-source regressions.

## Live execute 2026-08-27 — current-source packet (not 5695afec)

Implementation parent: `012ea4d` (`fix(272): send X-Session-Id on OpenAI chat completions`)
on `feat/verdict-operational-loop`. This demonstration does **not** reuse the
T042 packet bound to `5695afec`. Adaptive-state snapshot deletions were restored
and were not committed.

Proof classification for this section: **LIVE-PROVEN** unless a row says
otherwise.

### CHK001/CHK002 — source binding and dirty-digest recompute

Recorded immediately before packet create and live execute, working tree clean
except gitignored packet/receipt files:

| Field | Value | Proof |
|---|---|---|
| Commit | `012ea4d6ff8525a5889d858aae5b8bcbb4c4aaf4` | LIVE-PROVEN |
| Branch | `feat/verdict-operational-loop` | LIVE-PROVEN |
| `capture_worktree_digest` pass 1 | `sha256:ec81c6c13ee91d073a43bb482fd6af9660472361228ca753bcc6057fabc7c0ce` | LIVE-PROVEN |
| `capture_worktree_digest` pass 2 | `sha256:ec81c6c13ee91d073a43bb482fd6af9660472361228ca753bcc6057fabc7c0ce` | LIVE-PROVEN (identical recompute) |
| Packet `source.dirty_digest` | same digest | LIVE-PROVEN |
| Porcelain-v1 `-z` SHA-256 (empty status) | `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` twice | LIVE-PROVEN |
| `uv.lock` | `sha256:218ea7f760ffb24d7892a2673051fc002ded9d1b0d8ea4fcf5aef23ca99027a1` | LIVE-PROVEN |

### Gateway observation (settings not mutated)

| Field | Observation | Proof |
|---|---|---|
| OmniRoute version | `3.8.49` via MCP `omniroute_get_health` | LIVE-PROVEN |
| `GET /api/settings` `taskRouting.enabled` | `false` | LIVE-PROVEN |
| `taskRouting.detectionEnabled` | `false` | LIVE-PROVEN |
| Settings writes | none | LIVE-PROVEN |
| `omniroute quota` | `No quota information available.` | LIVE-PROVEN UNKNOWN |
| HTTP `GET /v1/models` | timed out, 0 bytes | LIVE-PROVEN (catalog flaky) |
| MCP catalog | `gemini/gemini-2.5-flash` listed; chat POST later 404 | LIVE-PROVEN conflict retained |
| Quota/headroom in Verdict evidence | omitted / `None` | LIVE-PROVEN UNKNOWN |

Re-read after execute: `enabled=false`, `detectionEnabled=false`. Unchanged.

### X-Session-Id

`PatchExecutor` default `session_id` is `verdict-operational-loop`.
`openai_probe_transport` sends `X-Session-Id` on `POST .../chat/completions`.
SOURCE-ONLY for the unit tests; LIVE-PROVEN that live OpenAI-compatible calls
from this session used that header on probe/completions (`X-Session-Id:
verdict-operational-loop`).

### CHK021 — second fallback refused (LIVE-PROVEN)

Packet `.verdict/packets/headroom-second-fallback-r1.json`
(`headroom-second-fallback-r1`, integrity
`sha256:62b9618038363ee4c5861ddbe01e099b3c5ad88e3a82bbebb05e1c97173d123e`),
bound to `012ea4d` / dirty digest above.

First admitted route: concrete non-primary `gemini/gemini-2.5-flash` at
`http://127.0.0.1:9/v1` (connection refused). `refresh_fallback` designated
`claude/claude-haiku-4-5-20251001` with `primary=True` and the same dead
base URL. The loop admitted **one** fallback. A second fallback was not
appended even though `refresh_fallback` remained willing (`refresh_calls`
length 1). Terminal `truthful_failure`. Owned files unchanged.

| Attempt | Requested | Class | Reason |
|---|---|---|---|
| 1 | `gemini/gemini-2.5-flash` (`primary=false`) | `worker_failed` | `URLError` connection refused |
| 2 | `claude/claude-haiku-4-5-20251001` (`primary=true`) | `worker_failed` | `URLError` connection refused |
| 3 | refused | terminal bound `index < 2` | no third route |

Receipts: before_inference `rcpt-5444431550974da5a0a313d6895bba74`; terminal
`rcpt-a2e9e2f06527465e87b4563eabc89ab2`.

### CHK026/CHK027/token_budget/authority/usage — post-`1a78b51` live rows

Inspected `.verdict/receipts.db` scope `operational-loop` after execute.

Packet `headroom-unknown-r9` (gemini 404 then haiku patch-apply failure) already
kept:

- context `token_budget=4096`, `used_tokens=1462`, `compiled_prompt=[REDACTED]`
- context unit `autodev:authority` included; provenance `authority=compiled`
- attempt usage integers not redacted (haiku `prompt_tokens=2877`,
  `completion_tokens=1764`, `total_tokens=4641`)
- provenance `authority=observed` on execution rows

Packet `headroom-unknown-r10` (completed) kept the same fields plus usage on
both attempts (see table below). Nested `context_receipt.decisions[].input_tokens`
remain `[REDACTED]` (not on `_PACKET_RECEIPT_ALLOWLIST`); that is named, not
omitted.

### Completed work-unit execute (LIVE-PROVEN)

One command:

```text
uv run verdict autodev packet execute --packet .verdict/packets/headroom-unknown-r10.json --repo . --allow-live --prefer-non-primary --primary-fallback claude/claude-haiku-4-5-20251001 --json
```

Packet `.verdict/packets/headroom-unknown-r10.json` (`headroom-unknown-r10`,
integrity `sha256:9cb80296c0b74547deecc9f2411665eb6d194ce21b0ab60977982be25291e564`).
Evidence digest `sha256:8b75c55151434b2da31a94decadbb9c466ee11ab9160eab62f1b13124d46234a`.
`--primary-fallback` stamped `primary=True` for
`claude/claude-haiku-4-5-20251001`. First admitted route was concrete
non-primary `kimi-coding/kimi-for-coding` (`primary=false`). No `auto/*`.

| Field | Value |
|---|---|
| Terminal | `completed`, proof_level `live-proven` |
| Fallbacks used | 1 of max 1 |
| Context | digest `sha256:db5f9a88b248336e6deaae4e23cc1298e735033626321e020dafd3e24668ae19`; **1462 / 4096** tokens |
| Attempt 1 | requested `kimi-coding/kimi-for-coding` → actual `kimi-for-coding`; `identity` fields distinct; `worker_failed` (`git apply --check`); usage 2358/491/2849; isolated worktree; zero leaked files |
| Attempt 2 | requested `claude/claude-haiku-4-5-20251001` → actual `claude-haiku-4-5-20251001`; `primary=true`; verified=true; usage 2877/4294/7171 |
| Changed paths | `verdict/headroom.py`, `tests/test_headroom.py` only |
| Artifact receipt digest | `sha256:d5bdcddd1391ff1bb8015ef16404ef5c76e79c48f59fcc4fcb847ed236973f38` |
| Checkpoints | `before_inference=rcpt-0a4825ac881f4e91a78889382b8c4cf6`; terminal `rcpt-1f07d2b0cae74dcb8281cc0d9c33b6b1` |
| Verification | trusted argv `.venv/bin/pytest -q tests/test_headroom.py` in attempt tree: 5 passed |
| Quota/headroom | UNKNOWN |

Patch substance: `headroom_is_unknown(result)` is True iff `result is None`;
`__all__` exports it beside `check_headroom`; tests cover None / `(True, 0.0)` /
`(False, 100.0)`. Post-replay operator stripped one W293 docstring blank-line
space so ruff stays green; that is not a second worker attempt.

Prior r9 (same source binding): first route `gemini/gemini-2.5-flash` HTTP 404
while MCP catalog listed it as available — catalog vs runtime conflict preserved
in this record, not merged. Haiku then produced an un-applicable patch; terminal
`truthful_failure`.

### CHK024 — resume idempotency (LIVE-PROVEN)

Immediate replay of the same CLI command on the completed r10 packet:

```text
resumed=true, terminal_state=completed, proof_level=live-proven
fallback_count=0
checkpoints.before_inference=rcpt-0a4825ac881f4e91a78889382b8c4cf6
receipt_ids = the same five ids, no new rows
```

Receipt count stayed 5. No second `before_inference`, no duplicate terminal
row, no additional attempt, no extra fallback.

### Focused gates after the live patch

```text
uv run pytest -q tests/test_headroom.py tests/test_patch_executor.py tests/test_probes.py tests/test_autodev_operational_loop.py
# 73 passed
uv run ruff check verdict/headroom.py tests/test_headroom.py verdict/probes.py verdict/patch_executor.py
# All checks passed
```

### Limitations (this window)

- HTTP catalog GET timed out; MCP catalog and chat POST disagreed for
  `gemini/gemini-2.5-flash` (404). — LIVE-PROVEN
- First successful worker was the designated primary-subscription fallback after
  the non-primary kimi patch failed `git apply --check`. — LIVE-PROVEN
- Context-pack `used_tokens` (1462) stayed under `token_budget` (4096); provider
  `usage.total_tokens` on the fallback call (7171) is a different meter and is
  recorded, not treated as the pack budget. — LIVE-PROVEN
- Nested context-decision `input_tokens`/`output_tokens` remain redacted. — LIVE-PROVEN

### CHK014 — live r10 worker pack inventory (LIVE-PROVEN)

Inspected `.verdict/receipts.db` context receipt `rcpt-8f4ea3f1fad741f1bf59b76c782d1b1f`
(`packet-context:headroom-unknown-r10:sha256:db5f9a88…`). `compiled_prompt=[REDACTED]`.
`omissions=[]`. Included units only:

| unit_id | action |
|---|---|
| `autodev:objective` | include |
| `autodev:repository_instructions` | include |
| `autodev:acceptance` | include |
| `autodev:authority` | include |
| `autodev:non_goals` | include |
| `autodev:owned_source:tests/test_headroom.py` | include |
| `autodev:owned_source:verdict/headroom.py` | include |
| `autodev:relevant_examples` | include |

No chat-transcript unit, no unrelated repository paths. Token pack **1462 / 4096**.

### CHK016 — advisory worker self-report beside trusted verification

Live r10 attempt receipts (`rcpt-fdd7322…`, `rcpt-0201894…`) store `verified` as the
deciding bit and do **not** contain a separate `worker_self_report` object
(LIVE-PROVEN gap on those rows).

Shipped `run_packet_autodev` now persists both on every attempt receipt:

- `worker_self_report.outcome` + `role=advisory`
- `trusted_verification.decided` + `role=deciding`

Trusted argv still overwrites `verified`. Fixture
`test_worker_self_report_is_advisory_when_trusted_verification_fails` proves
`outcome=applied` cannot complete when verification fails. — SOURCE-ONLY /
FIXTURE-ONLY until a new live execute writes post-change rows.

### Admission composition (T044 MEDIUM residual)

Live CLI path remains operator `--primary-fallback` → `designated_primary_fallback`
(LIVE-PROVEN at `012ea4d` / r10). Production `run_packet_autodev` now calls
`CandidateEvidence.to_admission_record` when `refresh_fallback` returns evidence.
Brand-only evidence cannot admit as fallback
(`test_refresh_fallback_composes_primary_from_candidate_evidence`). — SOURCE-ONLY /
FIXTURE-ONLY for the evidence composer; operator designation remains the sole
live-proven composition path.

### CHK004 leftover unlabeled historical sections (limitation)

The following earlier sections are **not** rewritten to one-label-per-claim:
Current state, Pre-US1, US1 red-test ledger, T042/T043 2026-08-24 narratives.
They remain mixed recovery-era prose. This closeout does not treat them as
Phase 1 live-proof claims.

## Residual closeout — post-`9401cad` source (CHK004/014/016)

Parent of this text is worktree `feat/verdict-operational-loop` after
`9401cad`. OmniRoute after operator restart: version `3.8.49` (MCP health),
`taskRouting.enabled=false`, `detectionEnabled=false` (`GET /api/settings`,
no writes). — LIVE-PROVEN for this window.
