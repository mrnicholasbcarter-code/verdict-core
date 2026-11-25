# Design

## Context

`verdict orchestrate` dispatches coding work through the `HarnessAdapter` /
`HarnessRegistry` contract (`verdict/orchestration/harness.py`,
`verdict.harness/v1`). Two adapters are registered today:
`PrimeHeadlessAdapter` (`prime-headless`) and `DirectGatewayAdapter`
(`direct-gateway`, default). Both are answer-only: they issue a single API call
and return text. Neither can run tests, lint or repo gates after editing files.

`verdict deliver` runs (#1269) are BLOCKED because one-shot workers cannot run
their own tests and lint. Fixing the route-environment issues (#1276, #1214)
does not solve this: even with a correct environment, an answer-only worker
cannot run a verification loop. The root cause is the executor kind, not the
environment.

OpenCode (opencode.ai, v2.3.2) runs a full edit–test–fix loop inside a
repository before returning. It accepts a provider endpoint and a model name,
supports `--standalone` (private per-process server, isolating the session DB
from the shared `~/.local/share/opencode/opencode.db`), and emits structured
JSON output with `--format json`.

**Code fact (verified):** `harness.py` registers `DirectGatewayAdapter(default=True)`.
ADR-041 declared `prime-headless` as the initial default; the
`default-executor-direct-gateway` OpenSpec change moved the code default to
`direct-gateway` before this ADR. ADR-053 Decision 2 first made `opencode` the new default;
the operator amended it on 2026-11-24: `opencode` merges opt-in (`--executor opencode`) and
`direct-gateway` stays the default until a live smoke passes after #1313. The live smoke of
this change (task 2.8) showed why: `verdict serve` refused all 13 of the worker's requests
before any upstream call (11 by the escalation keyword scanner, whose branch of `route()`
returns before the route pin is applied; 2 by the capability gate, which had no metadata for
streaming and tools on the relay path). The serve fix is #1313, with the signed per-attempt
assignment: a worker request with a valid assignment is pinned before serve classifies
anything itself.

## Goals / Non-Goals

**Goals:**

- Register `OpenCodeAdapter` in `DEFAULT_REGISTRY` as an opt-in worker executor for
  `verdict orchestrate` (`--executor opencode`); `direct-gateway` stays the default.
- Build `OpenCodeExecutor` that runs `opencode run --auto --standalone
  --format json --model verdict/<route_id>` with per-attempt isolated config
  (`OPENCODE_CONFIG_DIR` → temp directory).
- Route all worker model calls through `verdict serve` (:8000); never direct to
  OmniRoute (:20128). Add per-request route pinning to serve if not present.
- Declare all nine `HarnessCapability` facets explicitly (ADR-041 requirement).
- Pin OpenCode at v2.3.2; add a `verdict doctor` version warning.

2026-11-24: the autodev model-tier rung moved to issue #1301 (OC2), which will carry its own OpenSpec change, after cross-family review found isolation and retry defects.

**Non-Goals:**

- Interactive OpenCode sessions (outside scope of this story).
- MCP server use inside the worker (disabled by `--standalone`).
- Any change to `verdict autodev` (PatchExecutor stays its executor here; see the dated note above).
- Changing the planner executor (`--planner-executor` stays `direct-gateway`).
- Full shell command allow-listing inside OpenCode (operator's explicit choice
  not to restrict shell; see Decision 4 / ADR-053).

## Decisions

The eight operator decisions (D38 / ADR-053) govern this change. Implementation
details below are drawn from the research and are not operator decisions.

**Route enforcement (Decision 1):** The OpenCode config written per attempt sets
the provider base URL to `http://127.0.0.1:8000/v1`. The model entry maps
`verdict/<route_id>` to `<route_id>`. This ensures every model call from the
worker reaches `verdict serve`, which records the attempt and applies admission
and cost rules. If `verdict serve` does not today pin per-request route identity
for relay calls, adding that is part of the build.

**Capability facets (design detail):** The proposed `OpenCodeAdapter` capability
declaration:

| Facet | Level | Rationale |
|---|---|---|
| `worker_launch` | SUPPORTED | Core capability |
| `interactive_attach` | UNSUPPORTED | No interactive mode in this adapter |
| `model_switching` | SUPPORTED | `--model verdict/<route_id>` per attempt |
| `context_injection` | PARTIAL | The task is delivered as an instructions file in the attempt's config directory (nothing in the worktree); not all context pack formats are supported |
| `resume_checkpoint` | UNSUPPORTED | `--standalone` per-attempt; no cross-attempt session resume |
| `mcp` | UNSUPPORTED | `--standalone` mode; MCP not activated |
| `hooks` | UNSUPPORTED | No hook interception in this adapter |
| `tool_interception` | UNSUPPORTED | No tool interception |
| `event_stream` | PARTIAL | `--format json` emits structured JSON; not a full event-stream integration |

**Per-attempt config isolation (design detail):** Each attempt writes a fresh
OpenCode config to a temp directory, sets `OPENCODE_CONFIG_DIR` for that
process, and deletes the temp directory on exit. This prevents cross-attempt and
cross-worker session DB contamination. The config contains:

- Provider entry: `verdict`, base URL `http://127.0.0.1:8000/v1`, API key from the signed
  per-attempt assignment, which is the worker's only credential (`{env:VERDICT_WORKER_API_KEY}`;
  the serve bearer token is never given to the worker, and no key value is written to disk or logs).
- Model entry: `verdict/<route_id>` mapped to `<route_id>`.
- Policies: deny all `provider.use` actions; allow only `verdict`. Prevents the
  worker from calling openrouter, bedrock, or any other provider directly.
- Permissions: `external_directory` deny (V2 action name). Prevents the worker
  from opening directories outside the worktree as project roots.
- Agents: `title.model` pinned to `verdict/<route_id>` so the title summariser
  cannot pick its own model.
- Instructions: the task file `verdict-task.md` in the attempt's config directory (the task
  text), plus `AGENTS.md` when present.

No secrets appear in the config file or in logs. The API key is passed as
`{env:VERDICT_WORKER_API_KEY}` so the value is never written to disk.

Subprocess output is untrusted and may echo the key. Before any stderr or NDJSON text
(error, text, session id) reaches `WorkerTerminal`, events or logs, the executor replaces the
worker key value and any `Authorization: Bearer` value with `[REDACTED]`, before truncating, so
a key cannot survive as a fragment.

> **Deviation note (2026-11-23):** The original spec said `OPENCODE_CONFIG` →
> temp file; the implementation uses `OPENCODE_CONFIG_DIR` → temp directory,
> which is what OpenCode v2.3.2 reads for per-attempt isolation. `OPENCODE_CONFIG`
> (a single-file path) also exists in v2.3.2 but was not used because the
> directory form is what the binary expects when running `--standalone`. This
> deviation is intentional and correct.

**Worker identity (design detail):** Superseded by the change `serve-worker-assignment`
(2026-11-24). The worker header no longer selects a route and `WorkerAssignedRoute` is removed.
A worker's route comes from a verified signed assignment (its bearer); the request's `model` must
equal the signed route, or the request is refused 400 before routing. The relay hands the verified
assignment to admission as a typed server-set input; the assigned lane narrows to the signed route
right after the active and cooling steps, runs every per-route gate on it, and mints its
route-bound proof with the same `_chosen_route_proof` call as for a chosen route; there are no
fallback routes. A route the gates drop is denied with no proof minted, naming the check that
dropped it. The relay only verifies that outcome and never rewrites a worker's decision, and it
clears the alternatives of any accepted worker decision (defence in depth).

**Execution-path requests are rejected (operator decision, 2026-11-24):** a worker request that
carries `execution_path_request` is refused with 400 before any routing, because the
execution-path flow selects its own offer and so cannot pin the assigned route. Pinned
execution-path support for workers is not built. The relay checks this first (present, whatever
the value, on both relay surfaces); `IntelligenceService.route` also refuses a worker-assigned
route combined with `execution_path_request`, so no other caller can select a worker's route
through that flow. This rejection is the contract, not a gap.

2026-11-24: the worker held the serve token and the route was pinned by the worker header until #1313 story 1; that is superseded (`serve-worker-assignment`): serve now accepts only a signed assignment from a worker and refuses the bare header, and the executor now signs one per attempt (#1325, see "Signed assignment per attempt" below). (#1313, OC1b, carries the signed per-attempt assignment and a prompt-only task delivery; the real dispatch-origin check is done, see "Dispatch origin" below (#1326); the task is now an instructions file in the config directory, #1327.)

**Worker credential and pre-dispatch check (design detail):** The worker's credential is the signed
per-attempt assignment of "Signed assignment per attempt" below, never serve's own bearer token and
never the OmniRoute gateway key. It is set in the child environment only under the configured key
name (the config holds the `{env:...}` reference) and is covered by redaction (its payload and tag
too). Serve's own token is resolved by Verdict (`serve_service.resolve_serve_token`: the
environment's `LLMGATE_AUTH_TOKEN`, the credential store, then `serve.env`, in the order `verdict
setup` uses) and is used in the orchestrator's own process for one thing: the readiness check
below. Before an attempt writes or launches anything the executor signs the assignment (a missing
run id or node id, an attempt timeout that is not a positive finite number or whose lifetime
would exceed 1,800 s, and a missing, unreadable or short signing key each end the attempt here)
and checks, once, that the serve token resolves, that the `opencode` binary runs (`--version`)
and that serve is reachable and accepts the serve token (`GET /v1/guidance/status`, no redirect
followed; an assignment is not accepted on that path, so it is never sent there). Any failure is
one `harness_configuration:` message with a non-secret reason, which recovery maps to BLOCK (no
retry, no cooldown). Nothing falls back to the serve token.

**Signed assignment per attempt (design detail, 2026-11-25, #1325):** `OpenCodeExecutor.run` takes
`run_id` and `node_id` (the runtime passes both through `bounded_executor_call` and
`run_executor`, which offer an optional keyword only to an executor that accepts it, so every
other executor and fake is called exactly as before; `MixedExecutor` and `FaultInjectingExecutor`
forward them). The run id is the run directory's name, the id `run_started`, `graph.json` and the
run receipt carry, and the node id is the graph's. The executor signs
(`worker_assignment.mint_assignment`, key from `load_assignment_key`, which reads
`~/.config/verdict/worker-assignment.key` on every use) the route, the run id, the node id,
`iat` (now, whole seconds) and `exp` = `iat` + the attempt timeout rounded up + 60 s
(`ASSIGNMENT_MARGIN_SECONDS`: the assignment is dated just before the pre-dispatch checks, which
spend the attempt's budget, so it ends 60 s after the attempt's deadline: room for the last
request and the kill grace, and no more). The format allows at most 1,800 s, so an attempt whose
timeout plus the margin would exceed it fails closed before launch; it is never clamped (the
runtime caps an attempt at 900 s and gives the executor 10% less, so the real case is 945 s at
most). The format has no attempt-specific field, only whole seconds, so two attempts of one node on
one route signed in the same second would hold one credential and one digest, and serve's receipts
could not tell their requests apart (a request of the first that serve commits after the second
has started would be counted for the second). The executor therefore keeps the digests of its
assignments that have not expired and dates a repeat one second later (at most 30, well inside
the 60 s the verifier allows an `iat` to be ahead); the expiry follows from that issue time and is
never shortened. The signer is `mint_assignment` in `verdict/worker_assignment.py` next to the verifier;
`tests/worker_assignment_helpers.py` stays the independent signer, and a test proves both give
the same token for the same payload and key. The worker's header `X-Verdict-Worker` is no longer
sent: serve selects nothing from it.

Planner calls carry no node id and no assignment: `AnswerOnlyExecutor` (which every planner call
goes through) takes no ids and refuses an executor that is not answer-only, which includes
`OpenCodeExecutor`, so a planner call never reaches the signing executor; if it did, a missing
node id is a `harness_configuration:` block with nothing launched. No synthetic node id exists.

Serve answers any refused assignment with one 403 `authentication failed` (expired, tampered, a
key serve does not hold, dated ahead of its clock). That message and status alone do not prove
serve refused: serve forwards a provider's own buffered 401 or 403 unchanged, so a provider that
answers `{"error":{"message":"authentication failed"}}` looks the same to the worker. The
executor therefore treats a failure with that status (401 or 403) and those words (captured from
the real OpenCode 2.3.2, `tests/fixtures/opencode_stdout`) as a candidate only, and decides after
the worker is gone, from serve's own receipts (read back as below): serve writes a root for every
request that passes its door and reaches routing, before anything is sent upstream, and none for
one it refuses at the door. The
attempt ends `harness_configuration:` (a block, no cooldown) only when the read succeeded and
serve holds no request under the attempt's assignment. A request recorded under it means serve
admitted the assignment, so the error is the upstream's; receipts that cannot be read (not
configured, in memory, missing, unreadable, over the row limit or out of time) leave the origin
unproven. In both cases the failure stays the provider's and recovery handles it as before, with a
cooldown for the route's provider. The cost is accepted: when the orchestrator cannot read serve's
receipts (an orchestrator and a serve that read different `HOME`s, the usual cause of a refused
assignment, may also read different receipt databases) a real refusal is read as the provider's,
and a refusal after an earlier request of the same attempt was admitted cannot be told from the
provider's either. Telling them apart without serve's receipts needs serve to say so in its
answer, which this story does not change.

**What serve recorded, read back into the run receipt (design detail, #1325):** serve writes one
evidence root per relay request to its receipt database (`VERDICT_RECEIPTS_DB`), naming the
assignment by its digest (`routing_decision.receipt.worker_assignment`) and, for a request that
required `streaming`, the waiver (`requirement_waivers`). After the worker's process group is gone
the executor reads that database back read-only (`mode=ro`, never created or migrated; the path is
`VERDICT_RECEIPTS_DB`, the legacy `VERDICT_EVIDENCE_DB`, then `serve.env`) and counts the roots
that carry its assignment's digest, looking only at rows added since a mark taken just before the
worker started (the database grows with every request and holds large payloads). The result is on
the terminal as `worker_assignment` (the digest, never the credential, and `serve_receipts`:
`status` `read` with `requests`, `requests_waived` and the distinct waivers, or `unavailable`
with a reason and every figure `null` when the database is unconfigured, in memory, missing,
unreadable, over 10,000 matching roots or out of the 2 s read budget). `unavailable` is never
reported as "no waiver" and never fails an attempt that worked. The runtime puts it on the
`terminal` event, `receipt.json` projects it onto the attempt (only when present, so receipts of
other executors are byte-identical), and `verdict run-receipt` marks an attempt `streaming
waived` or `serve receipts unavailable`.

**Serve address (design detail):** `--serve-url` on `verdict orchestrate`, else
`VERDICT_SERVE_URL`, else the serve unit's own address (`serve_service.DEFAULT_ORIGIN`,
`http://127.0.0.1:8000`); there is no other copy of that default. The address becomes the
per-attempt provider `baseURL`.

**Dispatch origin (design detail, 2026-11-24, #1326):** the executor no longer declares
`LOCAL_EXECUTOR_ORIGIN` (the stopgap, which matched any bound origin): its workers send, through
serve. It reports the serve origin it really calls, the address it resolved for the per-attempt
`baseURL` (`resolve_serve_origin`), as a `WorkerServeOrigin`. That is a `str` subclass in
`contracts.py` and the type is the declaration: it survives `MixedExecutor` and
`FaultInjectingExecutor`, which delegate `executor_gateway_origin`, and a URL read from a
configuration file or reported by another executor is a plain `str` that cannot claim it.
`DispatchAuthority` gains `worker_serve_origin` (default None); `verdict orchestrate` fills it
from the same inputs the executor uses (`--serve-url`, `VERDICT_SERVE_URL`, the unit's address),
and an address that is not an http(s) origin binds none. `dispatch_origin_problem` is the one
decision the runtime makes before the permit: no origin is `executor origin unknown`; a
`WorkerServeOrigin` is compared (normalised, both sides parseable) with `worker_serve_origin`
only, never with `gateway_origin`, and is refused as `executor origin mismatch` when they differ
or when none is bound; every other origin is compared with `gateway_origin` exactly as before. The
permit still binds the gateway (serve's relay checks the permit against its own upstream), so the
recorded `dispatch_permit` evidence and its redaction are unchanged. Not done here: serve's own
proof that it forwards to the permit's gateway (the signed per-attempt assignment is built, see
"Signed assignment per attempt").

**Recorded cost (design detail):** OpenCode reports a cost of 0 for a custom provider with no
pricing, which is not "free". `cost_usd` is recorded only for a real positive figure across
every step; otherwise it is `null` with a `cost_note` (also written to the attempt evidence).
Tokens are always recorded. A structured `error` event keeps its HTTP `status` and
`retryAfterMs`, so recovery sees an upstream 429 or 503 as the reroutable failure it is.
When the text has no structured status (stderr, or a message with no `status` field), a status
is read only from a number marked as one (`HTTP 429`, `status code: 401`, `429 Too Many
Requests`); a retry delay or an attempt count in the text is not a status, because a wrong 429
would send the route into a provider cooldown.

**Cancellation (design detail):** the pre-dispatch checks block (the `--version` probe and one
HTTP request), so they run in a thread. Cancelling the await cannot stop a thread, so on a cancel
the executor sets an event, the probe is killed and reaped within a poll interval, no further
check starts, and the cancellation completes only after the thread has returned (the same
boundary the pre-dispatch checks keep). A serve request that has already started is waited for
(it is bounded by 5 s and is read-only).

**Open point:** Whether to pass the API key via an env var or via the OpenCode
config's credential field. Both keep it out of the command line and logs; the
builder resolves this during implementation.

**WorkerTerminal mapping (design detail):** The executor maps OpenCode's JSON
output onto `WorkerTerminal`:

- `output`: final assistant text from the JSON stream.
- `model`: served model identity from the JSON output or `opencode session
  export <attempt_id>` (confirmed during build).
- `stop_reason`: from the JSON output, or `"exit_nonzero"` on non-zero exit.
- `session_ref`: the `--session <attempt_id>` value.
- `usage`: `AttemptUsage` populated from JSON usage fields.
- `harness`: `"opencode"`.
- `executor_kind`: `"opencode"`.
- `served_check`: the `ServedCheck` result confirming route identity.

**Failure classification (design detail):** Four failure modes:

1. `timeout` — attempt killed at 900 s.
2. `idle_kill` — attempt killed by `VERDICT_WORKER_IDLE_SECONDS` stall.
3. `exit_nonzero` — non-zero exit code from `opencode run`.
4. `malformed_json` — `--format json` output cannot be parsed.

Two further bounds protect the orchestrator. Worker output (stdout plus stderr) kept in memory
is capped at 16 MiB per attempt (`output_limit`; the process group is killed). The
pre-dispatch checks count against the attempt timeout: the binary probe (15 s at most) and the
serve readiness check (5 s at most) each get only what is left of the attempt's budget, so a hung
probe cannot hold an attempt past its deadline; the worker gets only the remainder, and nothing
is spawned when none is left.

**Task delivery (design detail, #1327):** The task text is written once, as the instructions
file `verdict-task.md` beside `opencode.json` in the attempt's own temp config directory, and
that config lists it in `instructions` (with the worktree's `AGENTS.md` when there is one).
The prompt is one short, constant ask on stdin (OpenCode reads its message from stdin when it is
given none), so no task text is sent twice, no text nears the limit of one argument, and a task
of any size is delivered whole.  `--title` is given so OpenCode makes no second, title-agent
request on the route.  Nothing is written into the worktree: no `.verdict-context.md`, no
`BYPRODUCT_EXCLUDES` entry for it and no git probe, so a repository that tracks a file of that
name is left alone.  OpenCode skips a listed instructions file that is missing without a word, so
just before launch the executor checks that the file is a readable, non-empty regular file; a
task that cannot be encoded, a full disk or a file that is not there ends the attempt
`config_write_failed:` before any worker starts.  The temp directory, and the file with it, is
removed on every way out.

**Concurrency under `--max-parallel` (design detail):** Each attempt runs in its
own temp directory for the OpenCode config and its own node worktree. The
`--standalone` flag prevents shared server state. Concurrent workers are safe as
long as each uses a distinct `--session <attempt_id>` and a distinct temp config
path; both are guaranteed by the executor.

**Owned-files enforcement (design detail):** After an OpenCode attempt returns,
Verdict computes the diff relative to the node worktree head and checks that all
changed files are within the owned-files list. A diff outside owned files is
rejected and classified as a failure before verification runs.

## Risks / Trade-offs

- **Shell commands are not restricted** (Decision 4). A buggy or adversarial
  prompt could cause the OpenCode worker to run arbitrary shell commands inside
  the worktree. Verdict's owned-files check and the node's post-attempt
  verification are the enforcement layer. The risk is accepted explicitly.
- **OpenCode is a third-party tool.** Its licence and terms must be confirmed
  before the first build task ships (Decision 5); the licence placeholder in
  ADR-053 is to be filled by that task. Any upgrade resets the live-check
  requirement.
- **Per-request route pinning in `verdict serve`** needed more than a relay change. The
  relay never honoured `allow_client_model_override` (the flag is only stored on the
  service, and still is), and rewriting the model after admission cannot work because
  admission mints a route-bound proof only for the route it selects. Pinning therefore
  happens inside admission (see "Worker identity" above), so the receipt attests the
  route that was admitted and had its proof minted, and a route that is not admitted can
  never be selected. The route is the one Verdict dispatched: the executor signs it into the
  assignment, serve pins the request to it and refuses a request whose `model` differs, so a
  worker cannot name another route (the executor's signing is #1325; before it, the worker held
  the serve token and could name another *admitted* route).
- **JSON output field names** from `opencode --format json` are confirmed at
  build time. If usage fields are absent, `opencode session export <attempt_id>`
  is used as a fallback; the builder handles the case where both sources are
  incomplete.
- **No behaviour change for runs that do not name `opencode`.** `direct-gateway` stays the
  default, so a run without `--executor` is unchanged. A run that opts in with
  `--executor opencode` needs the signing key that `verdict setup --apply --allow host.serve`
  creates, and the orchestrator and serve must read the same one; without it the attempt ends in
  a `harness_configuration:` block before anything is launched. The default is not switched here
  (#1328): one live attempt through a spare serve passed on 2026-11-25 (task 2.19), the run on
  installed main is post-merge (task 8.1).
- **Known gap: the status matcher misses the `requests` forms.** An HTTP status in
  unstructured text needs an HTTP or status marker or its reason phrase right after the number,
  so `429 Client Error: Too Many Requests for url: ...` and
  `503 Server Error: Service Unavailable for url: ...` get no status and are not rerouted;
  accepted by the operator on 2026-11-24 and moved to #1313.

## Migration / rollback

- Existing `verdict orchestrate` runs with no `--executor` flag are unchanged: they use
  `direct-gateway`. To use OpenCode, pass `--executor opencode`.
- `prime-headless` users are unaffected.
- Making OpenCode the default is a later one-line change to the `DEFAULT_REGISTRY` default
  (`default=True` on `OpenCodeAdapter`, removed from `DirectGatewayAdapter`), with the
  default tests flipped, made in its own change (#1328) after a live run on installed main
  passes. If the adapter is defective before then, nothing needs reverting for runs that do not
  name it.

## ADR impact

- Amends nothing yet: [ADR-041](../../../docs/adr/ADR-041-generic-harness-contracts.md)'s
  default worker executor (`direct-gateway`) is unchanged until the default is switched.
  This change adds an opt-in `opencode` worker executor, and ADR-053 Decision 2 is amended
  (2026-11-24): the move of the default to `opencode` waits for a passing live smoke with
  serve honouring the signed assignment (serve's side merged in #1335, the executor's in
  #1325) and is made in its own change (#1328).
- Relates to [ADR-036](../../../docs/adr/ADR-036-goal-to-receipt-orchestration.md)
  (goal-to-receipt orchestration): worker output becomes richer (edit loop
  instead of answer-only).
- No impact on admission, ranking, or cost-policy ADRs.

## Security / trust implications

- **API key handling:** the worker's credential is the signed per-attempt assignment, and the
  general `verdict serve` bearer token never reaches the worker. Neither may appear in the
  OpenCode config file, in log output, or in process arguments: the config holds only the
  `{env:VERDICT_WORKER_API_KEY}` reference and the assignment is passed in that environment
  variable alone, which OpenCode does not echo (checked by tests that search every recorded
  value for the credential and for the serve token).
- **Worker isolation:** each attempt runs in a node worktree scoped by the
  runtime; workers cannot access the main repo or other worktrees. The
  `external_directory` deny in the OpenCode config is a defence-in-depth measure;
  the worktree isolation at the OS level is the primary boundary.
- **Shell command surface:** OpenCode workers can run arbitrary shell commands
  inside the worktree (Decision 4). The node's post-attempt verification,
  Verdict's owned-files diff check, and the repo gates run after the attempt and
  are the security boundary. Ticket text and worker output are untrusted data.
- **No secrets in config files or logs:** the per-attempt config temp file must
  not contain credentials that survive after the attempt. The temp directory is
  deleted on exit.

## Routing / context / memory implications

- **Routing:** all model calls from OpenCode workers go to `verdict serve`
  (:8000), which applies admission, ranking and cost rules. OmniRoute (:20128)
  is never contacted directly by the worker.
- **Context:** the task is delivered as an instructions file in the attempt's config directory
  (`verdict-task.md`), which the OpenCode agent reads via the `instructions` config field;
  nothing is written to the worktree. No memory is shared between attempts.
- **Memory:** no Hindsight or other memory plane interaction in the worker path;
  memory is a controller concern, not an executor concern.
