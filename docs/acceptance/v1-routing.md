# V1 acceptance matrix (A01-A30)

Status: **docs-only, no live calls executed.** Written by S12 (BOD-410) + S13 (BOD-439) per the
combined brief `prime-takeover-20261018/s12-s13-matrix-brief.md`. Source head for every command
and file:line citation below: branch `v1/acceptance-matrix` at `b120f484` in
`/home/nick/dev/verdict-core/.worktrees/v1-acceptance-matrix` (base `b120f484`, the Waves 1-6
integration head). Re-confirm all citations against the merged-main SHA before any live run; code
moves between this draft and the parent's merge.

This matrix is the V1 slice of [BOD-355](https://linear.app/bodanglin/issue/BOD-355/qualityp0-frontier-written-acceptance-scenario-matrix-per-capability),
not every historical Core capability. All 30 rows (A01-A30) are listed. Deterministic rows are
proven today by their owning story's focused pytest file(s), re-run at this head (raw pass/fail
below). Live rows (A23 live half, A29 installed-runtime provenance, A30 end-to-end vertical) are
**registered, not executed**: no live gateway call, no `--allow-live-probe`, no `verdict acceptance
run`, no three-cycle maintenance proof has been run by this story.

**Operator consent is already recorded** (BOD-342 comment, 2026-10-19 ~15:55Z, "yes and yes"):
(1) the installed-runtime cutover via S11, with rollback captured first, after the combined PR
merges to main; (2) BOD-410 live qualification on subscription and free routes only, with a hard
request cap. Consent is therefore NOT the open gate for A23/A29/A30. The named gate these three
rows stay `BLOCKED` on is **awaiting merge to main + install**: the live run must happen from the
merged-main checkout (A29/A30) and, for A29/A23's acceptance-run reflecting the real cutover,
after S11's operator-approved install step. Scope limits carried from the BOD-342/BOD-410 consent:
subscription + free routes only, no metered calls, a hard per-cycle request cap (this story's own
16 Core-requests/cycle ceiling is never raised to use the consent's headroom).

## Caps and constraints that govern every live row (ceilings, never knobs to raise)

- S12 per-cycle ceiling: **4 routes / 16 Core-requests / 4096 output tokens / 600 s wall**
  (`verdict/orchestration/verified_refresh.py` `MAINTENANCE_MAX_ROUTES=4`,
  `MAINTENANCE_MAX_REQUESTS=16`, `MAINTENANCE_OUTPUT_TOKENS=4096`,
  `MAINTENANCE_WALL_SECONDS=600.0`, `HARD_MAX_WALL_SECONDS_MAINTENANCE=600.0`; lines 168-183).
- `prove-at-rest once`'s own standalone config is up to **50 routes / 100 requests per cycle**
  (coordinator hard ceilings `HARD_MAX_ROUTES=50`, `HARD_MAX_REQUESTS=100`,
  `verified_refresh.py:142-144`), bounded further by `--max-requests`/`--max-wall-seconds` flags
  (defaults 300/600, CLI help below) — the BOD-297 decision on record is that prove-at-rest's chat
  phase takes `min(own cap, coordinator hard cap)`, never the full daemon default.
- No unbounded `test-all`. No new `auto/*` or `combo/*` route publication. Only subscription and
  free routes; no metered calls from this story.
- Global/pool concurrency ceilings: `HARD_MAX_CONCURRENCY=4` (`verified_refresh.py:145`);
  `MAINTENANCE_MAX_PER_PROVIDER=2` (`verified_refresh.py:168`).
- Every live attempt must be permit-committed (`verdict/orchestration/health_cache.py`
  `DispatchPermit`/`commit_dispatch`) and reserved against the shared coordinator ledger — this
  matrix does not invent a parallel lane.
- Evidence paths for any future live run live outside the repo under
  `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/`
  until `scripts/check_dod.py` validation passes; only then does evidence move under
  `docs/proof/dod/`.

## CLI help recorded at this head (literal, re-verify at merge)

Commands recorded with `PYTHONPATH=/home/nick/dev/verdict-core/.worktrees/v1-acceptance-matrix /home/nick/dev/verdict-core/.venv/bin/python -m verdict <cmd> --help`
at `b120f484`:

- `prove-at-rest once --help`: flags `--state-path`, `--json`, `--base-url`, `--interval` (daemon
  only), `--timeout`, `--allow-live-probe` (explicit consent), `--max-requests` (default 300),
  `--max-wall-seconds` (default 600), `--agentic-max-routes` (default 8), `--agentic-concurrency`
  (default 4), `--agentic-pool-concurrency` (default 1), `--agentic-wall-seconds`.
- `prove-at-rest census --help`: `--inventory-path` (required), `--json`, `--state-path`.
- `prove-at-rest status --help`: `--state-path`, `--json`.
- `route --help`: positional `task`, `--terse`, `--criticality {critical,high,medium,low}`,
  `--allow-offline`, `--allow-legacy-selector`.
- `serve --help`: `--port`, `--host`, `--dev`.
- `eligibility --help`: `--gateway`, `--scope`, `--prefer`, `--probe`, `--sync-visibility`,
  `--reasoning`, `--frontier`, `--provider-family`, `--verified`, `--status`, `--provider`,
  `--search`, `--page`, `--page-size`, `--no-refresh`/`--refresh`, `--include-unknown`,
  `--allow-metered`, `--json`, `--no-pager`.
- `acceptance --help`: subcommands `run`, `status`, `install-service`.
  `acceptance run --help`: `--gateway`, `--notify {off,dry-run,send}`, `--email`, `--keep`
  (default 48), `--json`.
- `doctor --help`: `--fix`, `--json`, `--preflight-timeout` (default 120).
- `receipt --help`: subcommands `list`, `show`, `export`.
- `models --help`: `--json`, `--provider`, `--search`, `--capability`, `--limit` (default 40),
  `--all`, `--inventory`.
- **`choose` EXISTS** (unlike the brief's example): `choose --help` -> `--task-class` (required),
  `--requires`, `--model`, `--candidates-json` (required for P0), `--json`. Not BLOCKED as a
  command; any row citing it is proven by `tests/test_acceptance.py`'s `choose` check plus
  `verdict/acceptance.py`'s `cli("choose", ...)` wrapper, not a bare invented invocation.

## Row format

Category | Preconditions | Exact command | Expected observable result | Anti-gaming check | Evidence | Story

### A01 — S04

- **Gap/requirement:** Inventory is broader than working supply; opaque/unmapped aliases
- **Falsifiable acceptance (contract text):** Mixed free/subscription/metered/unknown, opaque and malformed rows remain internally accounted for; only supported concrete equivalents deduplicate into execution.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_free_tier_admit.py tests/test_admit_snapshot_resilience.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A02 — S07

- **Gap/requirement:** Public catalog currently overstates eligibility
- **Falsifiable acceptance (contract text):** At age 599.999s publish matching basic proof; at 600s exclude it. GET performs zero provider/model calls and applies caller access.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_relay_admission.py tests/test_verified_models_projection.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A03 — S04 + S07

- **Gap/requirement:** Cold/corrupt authority and stale last-good
- **Falsifiable acceptance (contract text):** Missing/malformed/expired inventory produces 503; valid current empty projection produces 200/empty. Timeout, oversized body/row/page caps and repeated/inconsistent cursors cannot publish a truncated inventory or renew old evidence.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_admit_snapshot_resilience.py tests/test_authoritative_eligibility.py tests/test_verified_models_projection.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A04 — S03

- **Gap/requirement:** Evidence scopes, sibling/account extrapolation, late positives
- **Falsifiable acceptance (contract text):** Success on A does not verify B; persist denial epoch, restart, replay older/equal-time/conflicting positives and retain denial; later epoch-bound genuine proof may qualify a new decision; future timestamps fail.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_health_cache.py tests/test_dispatch_permits.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A05 — S04

- **Gap/requirement:** MCP optimistic defaults and schema/filter loss
- **Falsifiable acceptance (contract text):** Nonexistent provider, nested analytics, missing fields, ignored filters and partial failure yield scoped unknown; known real zero remains zero.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_free_tier_admit.py tests/test_eligibility_gateway_contract.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A06 — S01

- **Gap/requirement:** Credential resolver and fallback-port asymmetry
- **Falsifiable acceptance (contract text):** With only canonical/stored test credentials, every entrypoint resolves identical bound URL/key; no secret appears in errors/receipts.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_gateway_key_resolution.py tests/test_credentials_store.py tests/test_credentials_masking.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A07 — S02

- **Gap/requirement:** Raw caller flags weaken server authority
- **Falsifiable acceptance (contract text):** Raw chat and Responses flags/fake decisions/override headers cannot trigger legacy routing or override selected identity under production policy.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_serve_path_cutover.py tests/test_execution_path.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A08 — S02

- **Gap/requirement:** Typed requirement loss; divergent live entrypoints
- **Falsifiable acceptance (contract text):** Family/context/reasoning/tool/schema requirements survive each intended live entrypoint and reject inadequate routes. Offline catalog mode cannot authorize send. Test CLI serve and direct ASGI.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_execution_path.py tests/test_candidate_pool.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A09 — S05

- **Gap/requirement:** Advertised IDs receive synthetic health; qualification circularity
- **Falsifiable acceptance (contract text):** Advertised-only stays unknown; authorized cold-start probe can qualify a one-route provider; a >4-route stable backlog under renewal pressure rotates fully within N qualifying budgeted cycles.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_verified_model_refresh.py tests/test_census_passports.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A10 — S03

- **Gap/requirement:** Confirmation-to-send/retry revalidation
- **Falsifiable acceptance (contract text):** Barrier-race denial before permit commit: zero sends; denial after commit: classified possibly-sent/cancellation and no retry. Expiry/scope checks occur per permit. Last-good fallback plus failed current confirmation denies independently.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_dispatch_permits.py tests/test_api_dispatch_permits.py tests/test_worker_dispatch_permits.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A11 — S07

- **Gap/requirement:** Optional 60-second relay-cache availability cliff
- **Falsifiable acceptance (contract text):** Boot with absent/expired state returns 503; 20s reload/five-second deadline never blesses expired source; repeated failures remain unavailable, later valid refresh recovers without restart. Inventory refresh runs during long qualification.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_verified_models_projection.py tests/test_proxy.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A12 — S05

- **Gap/requirement:** JOIN/debounce lacks stable scope binding
- **Falsifiable acceptance (contract text):** Same route IDs on different gateway/config/profile/consent scopes never reuse outcomes; compatible joins perform one owner sweep, retain existing 10s debounce.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_verified_model_refresh.py tests/test_verified_refresh_consumers.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A13 — S05

- **Gap/requirement:** Mutable generation self-invalidates work
- **Falsifiable acceptance (contract text):** Owner reservation/result changes evidence revision only; real external revocation/config/profile change invalidates affected future work immediately.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_verified_model_refresh.py tests/test_verified_refresh_consumers.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A14 — S05

- **Gap/requirement:** Independent qualification producers multiply caps
- **Falsifiable acceptance (contract text):** Two processes plus census/manual/live-confirm demand over 9,000 IDs obey pre-screen and aggregate caps; exploration reservation survives renewal pressure; deadline joiners get explicit exhaustion/scope results. Request credits differ from LLM tokens.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_verified_refresh_consumers.py tests/test_census_passports.py tests/test_prove_at_rest_agentic_budget.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A15 — S05

- **Gap/requirement:** Crash/cancel/late writer accounting
- **Falsifiable acceptance (contract text):** Kill during reservation, permit commit/partial write, send and settlement; race recovery against late completion: fenced owner, one reservation/refund, MAYBE_SENT charge, no over-cap send or newer-denial overwrite.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_health_cache.py tests/test_verified_model_refresh.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A16 — S05

- **Gap/requirement:** Configured concurrency is serial today
- **Falsifiable acceptance (contract text):** <=4 Core global and <=1/known independent pool; any unknown topology takes exclusive gateway lane against both kinds. Test actual safe overlap, bounded waits/cleanup and explicit hidden-upstream limitation.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_verified_model_refresh.py tests/test_credential_pools.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A17 — S06

- **Gap/requirement:** Valid chat SSE finish/usage/DONE rejected
- **Falsifiable acceptance (contract text):** Normal finish→usage→DONE, multiple choices and tool-call terminal forward correctly; task remains unchecked until required next turn/checker.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_stream_terminal.py tests/test_stream_relay_wiring.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A18 — S06 + S09

- **Gap/requirement:** Failed/incomplete Responses become success
- **Falsifiable acceptance (contract text):** completed/failed/incomplete/abrupt EOF remain distinct through HTTP stream, terminal event and durable outcome; no EOF success synthesis.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_stream_terminal.py tests/test_task_profile.py tests/test_outcome_attempts.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A19 — S06

- **Gap/requirement:** Event bounds depend on chunk size
- **Falsifiable acceptance (contract text):** Replay identical frames byte-at-a-time and in one chunk, CRLF/multi-line/UTF-8 splits; valid results identical, oversized aggregate event always rejected.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_stream_terminal.py`
- **Expected observable result:** `1 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 1 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A20 — S06

- **Gap/requirement:** Priming wrapper leaks transport on cancellation
- **Falsifiable acceptance (contract text):** Cancel/close the full primed→validated→evidence chain at each boundary: upstream closes exactly once, one cancelled outcome, no retry after output.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_stream_relay_wiring.py tests/test_proxy.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A21 — S06

- **Gap/requirement:** Missing/rogue stream identity passes
- **Falsifiable acceptance (contract text):** Wrong buffered/header/SSE identities fail; explicit valid alias passes; missing E1 never mints proof; protected E2/E3 defaults cannot be downgraded by client data; no false model-attestation label.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_relay_identity.py tests/test_proxy.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A22 — S06

- **Gap/requirement:** Hidden retries, substitution and unsafe replay
- **Falsifiable acceptance (contract text):** 401/403/429/timeout/partial stream/tool interruption are bounded and classified; only authorized alternatives run; no duplicate tool effect; hidden-attempt guarantees marked unsupported.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_proxy.py tests/test_api_relay_context_length.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A23 — S06 + S12 (live half)

- **Gap/requirement:** Basic health conflated with capability/task correctness
- **Falsifiable acceptance (contract text):** Basic-only route fails tool/schema/context/coding/session requirement; independent failing or skipped check prevents task success even after transport 200.
- **Category:** Live (gateway/maintenance vertical)
- **Preconditions:** AWAITING MERGE TO MAIN + INSTALL (not consent — BOD-342 operator consent recorded 2026-10-19 ~15:55Z ("yes and yes"): (1) the installed-runtime cutover via S11, with rollback captured first, after the combined PR merges; (2) BOD-410 live qualification on subscription and free routes only, with a hard request cap. Consent is NOT the open gate for this row.). The live half cannot run from this worktree: it needs the combined PR merged to main and (for A23's `acceptance run` to reflect the real installed cutover) S11's operator-approved install step done, per the BOD-342 consent's own ordering (S11 cutover + rollback captured first). Scope limits from the consent: subscription + free routes only, no metered, hard request cap per cycle (this story's 16 Core-requests/cycle ceiling, never raised).
- **Exact command:** `Deterministic half: see test_map A23 above (PASSES). Live half (not run): `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m verdict acceptance run --gateway http://127.0.0.1:20128` then inspect the `eligibility` check's `independent_check` precision/recall and the `route`/`serve` checks for a basic-only route that fails a stronger task profile.`
- **Expected observable result:** Not observed. Would-be: 3 cycles each respecting 4-route/16-request/4096-output/600s caps; `acceptance run` exit 0 (green) or exit 3 (red, recorded) with `eligible_free > 0` when supply exists; insufficient supply recorded as explicit BLOCKED, never forced green.
- **Anti-gaming check:** No unbounded test-all; no new auto/opaque route; caps are ceilings only (never raised); insufficient supply must show BLOCKED not a fabricated pass; parent re-run required (author != confirmer, check_dod.py parent_confirmation).
- **Evidence:** None yet. Target path once merged to main, installed, and run: `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<cycle-id>-a23-raw.json` (outside repo) -> `docs/proof/dod/<story>.json` only after `check_dod.py` validates.
- **Status:** AWAITING MERGE TO MAIN + INSTALL (consent already recorded, BOD-342 2026-10-19 ~15:55Z); deterministic prerequisite code is present at this head

### A24 — S08

- **Gap/requirement:** Stale/null quality and missing installed schedule
- **Falsifiable acceptance (contract text):** One six-hour job installed; fake-clock refresh, supported mirror opt-in, partial/malformed source, exact/unmapped identity, null/stale provenance and recovery tested without network.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_metadata_service_state.py tests/test_doctor_installed_state.py tests/test_model_metadata.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A25 — S02 (S10 rechecks end-to-end regression)

- **Gap/requirement:** Candidate fuse and authority optimizer still prefer cost
- **Falsifiable acceptance (contract text):** Change only prices/class labels with the same authorization/evidence and explicit budget constraints: admitted set, shortlist, optimizer decision and public selection unchanged. Missing pricing is not an extra gate without a hard spend budget.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_candidate_pool.py tests/test_live_candidate_shortlist.py tests/test_security.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A26 — S09

- **Gap/requirement:** Every attempt and effective parameter join
- **Falsifiable acceptance (contract text):** Successful, rejected, failed, retried, cancelled and incomplete attempts retain request/decision/run/node/attempt/route links and normalized settings; no fabricated usage.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_outcome_attempts.py tests/test_outcome_records.py tests/test_autodev_outcome_attempts.py`
- **Expected observable result:** `3 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 3 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A27 — S10

- **Gap/requirement:** Durable sink is not a selection consumer
- **Falsifiable acceptance (contract text):** Restart and replay checked outcomes once: exact smoothed score,30-day cutoff/seven-day decay and task match reproduce; n<3 never demotes and score<.4 with n>=3 does. Transient/duplicate/incompatible-rubric rows add no quality samples; actual next admitted shortlist consumes it.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_outcome_shadow.py tests/test_strength_profiles.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A28 — S09 (S10 rechecks verified replay)

- **Gap/requirement:** Receipt integrity and as-of leakage
- **Falsifiable acceptance (contract text):** Tampered, duplicate, future and wrong-scope inputs rejected; later checker result joins append-only; replay reproduces the decision from frozen inputs.
- **Category:** Deterministic (component/offline fixture)
- **Preconditions:** Checkout at the citation head (or merged main); repo `.venv`/`uv` environment installed; no network/live gateway required — tests inject clocks/transports and use temporary state.
- **Exact command:** `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m pytest -q tests/test_outcome_shadow.py tests/test_orch_receipt.py`
- **Expected observable result:** `2 file(s) collected, all PASS, exit 0` — re-run at `b120f484`: all 2 file(s) in this row are part of the 41-file / 1824-test sweep that passed 1824 passed, 1 warning in 66.70s (2026-10-19, this worktree).
- **Anti-gaming check:** Tests use injected clocks/fake transports/temp state, never a live provider call (operator rule: no live/paid calls in tests). A green row here proves spec conformance and fixture coverage, NOT live end-to-end proof — the distinction this matrix itself exists to keep explicit (contract 'V1 acceptance remains explicit and falsifiable').
- **Evidence:** pytest raw output captured in this session; re-capture at merged-main SHA under `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<story>-<aid>-pytest.log` before any Done claim.
- **Status:** PROVEN AT THIS HEAD (re-run on merged main required before Done)

### A29 — S11 + S12 (live provenance)

- **Gap/requirement:** Three source copies / independent installed loops
- **Falsifiable acceptance (contract text):** Running entrypoint, imported modules, installed runtime and reviewed source have recorded matching build/config provenance; unexpected parallel loop/scope mismatch is diagnostic and cannot count as main proof.
- **Category:** Live (gateway/maintenance vertical)
- **Preconditions:** AWAITING MERGE TO MAIN + INSTALL (not consent — BOD-342 operator consent recorded 2026-10-19 ~15:55Z ("yes and yes"): (1) the installed-runtime cutover via S11, with rollback captured first, after the combined PR merges; (2) BOD-410 live qualification on subscription and free routes only, with a hard request cap. Consent is NOT the open gate for this row.). A29's live half specifically needs the S11 installed-runtime cutover executed (with rollback captured first) after the combined PR merges to main, per the BOD-342 consent item (1); doctor must then report the ACTUALLY INSTALLED runtime, not this worktree.
- **Exact command:** ``PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m verdict doctor --json` (installed runtime provenance rows) re-run from the ACTUALLY INSTALLED runtime (systemd units), not this worktree, after S11's cutover is operator-approved.`
- **Expected observable result:** Not observed. Would-be: 3 cycles each respecting 4-route/16-request/4096-output/600s caps; `acceptance run` exit 0 (green) or exit 3 (red, recorded) with `eligible_free > 0` when supply exists; insufficient supply recorded as explicit BLOCKED, never forced green.
- **Anti-gaming check:** No unbounded test-all; no new auto/opaque route; caps are ceilings only (never raised); insufficient supply must show BLOCKED not a fabricated pass; parent re-run required (author != confirmer, check_dod.py parent_confirmation).
- **Evidence:** None yet. Target path once merged to main, installed, and run: `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<cycle-id>-a29-raw.json` (outside repo) -> `docs/proof/dod/<story>.json` only after `check_dod.py` validates.
- **Status:** AWAITING MERGE TO MAIN + INSTALL — specifically the S11 operator-approved installed-runtime cutover with rollback captured first (consent already recorded, BOD-342 2026-10-19 ~15:55Z); deterministic prerequisite code is present at this head

### A30 — S12 + S13 (live vertical)

- **Gap/requirement:** No complete live vertical proof yet
- **Falsifiable acceptance (contract text):** After implementation: exact-main bounded real inventory→qualification→selection→execution→independent check→joined receipt→feedback; repeat maintenance across three cycles/freshness windows, parent reruns and records raw result. Insufficient supply is explicit blocked.
- **Category:** Live (gateway/maintenance vertical)
- **Preconditions:** AWAITING MERGE TO MAIN + INSTALL (not consent — BOD-342 operator consent recorded 2026-10-19 ~15:55Z ("yes and yes"): (1) the installed-runtime cutover via S11, with rollback captured first, after the combined PR merges; (2) BOD-410 live qualification on subscription and free routes only, with a hard request cap. Consent is NOT the open gate for this row.). A30's three-cycle live vertical needs the combined PR merged to main first; BOD-410 consent item (2) bounds the run to subscription + free routes only, no metered, with a hard per-cycle request cap (16 Core-requests here, never raised) once merge+install clears the gate.
- **Exact command:** `Cycle 1-3: `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m verdict prove-at-rest once --allow-live-probe --max-requests 16 --max-wall-seconds 600` spaced >= 600 s apart, each followed by `PYTHONPATH=$PWD /home/nick/dev/verdict-core/.venv/bin/python -m verdict acceptance run --gateway http://127.0.0.1:20128` and a `GET /v1/models` before/after capture.`
- **Expected observable result:** Not observed. Would-be: 3 cycles each respecting 4-route/16-request/4096-output/600s caps; `acceptance run` exit 0 (green) or exit 3 (red, recorded) with `eligible_free > 0` when supply exists; insufficient supply recorded as explicit BLOCKED, never forced green.
- **Anti-gaming check:** No unbounded test-all; no new auto/opaque route; caps are ceilings only (never raised); insufficient supply must show BLOCKED not a fabricated pass; parent re-run required (author != confirmer, check_dod.py parent_confirmation).
- **Evidence:** None yet. Target path once merged to main, installed, and run: `/home/nick/dev/verdict-core/.git/verdict-prime/missions/bod431/prime-takeover-20261018/live/<cycle-id>-a30-raw.json` (outside repo) -> `docs/proof/dod/<story>.json` only after `check_dod.py` validates.
- **Status:** AWAITING MERGE TO MAIN + INSTALL (consent already recorded, BOD-342 2026-10-19 ~15:55Z, scope: subscription + free routes only, hard request cap, no metered); deterministic prerequisite code is present at this head


## Summary

- 27 deterministic rows (A01-A22, A24-A28) re-run at `b120f484` in this worktree: all PASS
  (1824 tests, 41 focused files, 66.70s wall, 2026-10-19). Re-run required on merged main before
  any Done claim (operator rule: tests/CI/merge are never Done).
- 3 live rows (A23 live half, A29, A30) are **BLOCKED on "awaiting merge to main + install"**,
  NOT on consent: operator consent is already recorded (BOD-342, 2026-10-19 ~15:55Z, "yes and
  yes" — (1) S11 installed-runtime cutover with rollback captured first after the combined PR
  merges, (2) BOD-410 live qualification on subscription + free routes only with a hard request
  cap). The deterministic prerequisite code for S05 phase 2 (maintenance lane), S09 (attempt
  ledger), S10 (verified history), S11 (installed provenance) is present and tested at this head;
  the remaining gap is the merge-to-main + install step itself, then the bounded live run within
  the consented scope (subscription + free only, no metered, hard per-cycle request cap).
- No code was edited by this story. No live/network call was made. `uv.lock` was not touched.
