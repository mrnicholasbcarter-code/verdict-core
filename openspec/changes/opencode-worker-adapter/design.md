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
| `context_injection` | PARTIAL | Context pack delivered as a file in the worktree; not all context pack formats are supported |
| `resume_checkpoint` | UNSUPPORTED | `--standalone` per-attempt; no cross-attempt session resume |
| `mcp` | UNSUPPORTED | `--standalone` mode; MCP not activated |
| `hooks` | UNSUPPORTED | No hook interception in this adapter |
| `tool_interception` | UNSUPPORTED | No tool interception |
| `event_stream` | PARTIAL | `--format json` emits structured JSON; not a full event-stream integration |

**Per-attempt config isolation (design detail):** Each attempt writes a fresh
OpenCode config to a temp directory, sets `OPENCODE_CONFIG_DIR` for that
process, and deletes the temp directory on exit. This prevents cross-attempt and
cross-worker session DB contamination. The config contains:

- Provider entry: `verdict`, base URL `http://127.0.0.1:8000/v1`, API key from
  the serve bearer token (never written to disk or logs).
- Model entry: `verdict/<route_id>` mapped to `<route_id>`.
- Policies: deny all `provider.use` actions; allow only `verdict`. Prevents the
  worker from calling openrouter, bedrock, or any other provider directly.
- Permissions: `external_directory` deny (V2 action name). Prevents the worker
  from opening directories outside the worktree as project roots.
- Agents: `title.model` pinned to `verdict/<route_id>` so the title summariser
  cannot pick its own model.
- Instructions: path to `.verdict-context.md` in the worktree (context pack),
  plus `AGENTS.md` when present.

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

**Worker identity (design detail):** A request carrying the `X-Verdict-Worker` header must name
its assigned route in a non-empty string `model`. The relay hands that route to admission as a
typed input (`WorkerAssignedRoute`, set only by the relay and only when the header is present;
client JSON cannot supply it). `_decision_from_admit` selects it within the admitted set, only
when that exact route id is in `receipt.admitted` for this request, and mints its route-bound
proof with the same `_chosen_route_proof` call as for a chosen route; the provider, context pack
and receipt follow the pinned route and there are no fallback routes. A route outside the
admitted set is denied with no proof minted. The relay only verifies that outcome and never
rewrites a worker's decision. A missing, empty, null or non-string `model` is denied, and no
denial contacts upstream; a worker request never falls through to automatic selection.
An accepted worker decision comes only from this admission pinning, so a worker request has
no fallback attempts; the relay also clears the alternatives of any accepted worker decision
(defence in depth).

**Execution-path requests are rejected (operator decision, 2026-11-24):** a worker request that
carries `execution_path_request` is refused with 400 before any routing, because the
execution-path flow selects its own offer and so cannot pin the assigned route. Pinned
execution-path support for workers is not built. The relay checks this first (present, whatever
the value, on both relay surfaces); `IntelligenceService.route` also refuses a worker-assigned
route combined with `execution_path_request`, so no other caller can select a worker's route
through that flow. This rejection is the contract, not a gap.

2026-11-24: the worker holds the serve token and the route is pinned by the worker header until #1313; a worker could name another admitted route, never a non-admitted one. (#1313, OC1b, carries the signed per-attempt assignment, the real dispatch-origin check and a prompt-only task delivery; this change keeps `LOCAL_EXECUTOR_ORIGIN` and its documented stopgap, and `.verdict-context.md`.)

**Worker credential and pre-dispatch check (design detail):** The credential is serve's own
bearer token, resolved by Verdict (`serve_service.resolve_serve_token`: the environment's
`LLMGATE_AUTH_TOKEN`, the credential store, then `serve.env`, in the order `verdict setup` uses),
never an operator-exported worker variable and never the OmniRoute gateway key. It is chosen in
one small function (`_worker_credential`) so a signed assignment can replace it. It is set in the
child environment only under the configured key name (the config holds the `{env:...}`
reference) and is covered by redaction. Before an attempt writes or launches anything the
executor checks, once, that the credential resolves, that the `opencode` binary runs
(`--version`) and that serve is reachable and accepts the credential (`GET /v1/guidance/status`,
no redirect followed). Any failure is one `harness_configuration:` message with a non-secret
reason, which recovery maps to BLOCK (no retry, no cooldown).

**Serve address (design detail):** `--serve-url` on `verdict orchestrate`, else
`VERDICT_SERVE_URL`, else the serve unit's own address (`serve_service.DEFAULT_ORIGIN`,
`http://127.0.0.1:8000`); there is no other copy of that default. The address becomes the
per-attempt provider `baseURL`.

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
boundary the context-file write keeps). A serve request that has already started is waited for
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
is capped at 16 MiB per attempt (`output_limit`; the process group is killed). Context
preparation counts against the attempt timeout: each git probe (30 s at most) gets only what is
left of the attempt's budget, so a hung repository cannot hold an attempt past its deadline; the
worker gets only the remainder, and nothing is spawned when none is left.

**Context pack delivery (design detail):** The context pack and owned-files list
are written to `.verdict-context.md` in the node worktree before the attempt
starts, and the OpenCode instructions list references that file. The context
pack is Verdict's standard format; the builder confirms the exact injection
mechanism.
The file is written without following symlinks (a fresh regular file atomically replaces
whatever entry exists), because a repository can track `.verdict-context.md` as a symlink to a
path outside the worktree.
`.git/info/exclude` hides only untracked paths, so if the index already tracks `.verdict-context.md`
the attempt fails closed (`context_write_failed`) before the worker starts: replacing a tracked
entry would be staged by `git add -A` into the node deliverable. The same applies when git cannot
answer (missing, timeout, nonzero exit, unreadable index, bare repository); only git's own report
that the directory is not inside a repository counts as certainly untracked. Git decides under the
process environment, as the runtime's own git calls do, so `GIT_DIR`/`GIT_WORK_TREE` setups are
covered. Context preparation runs off the event loop (`asyncio.to_thread`) because the check can
take seconds and the loop is shared by every concurrent worker.
A cancelled attempt waits for that thread to finish before the cancellation propagates, so no
write to the worktree can outlive a cancellation that the caller has already seen complete.

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
  never be selected. It does not authenticate the worker-supplied route against the one
  Verdict dispatched: until #1313 a worker can name another *admitted* route.
- **JSON output field names** from `opencode --format json` are confirmed at
  build time. If usage fields are absent, `opencode session export <attempt_id>`
  is used as a fallback; the builder handles the case where both sources are
  incomplete.
- **No behaviour change for runs that do not name `opencode`.** `direct-gateway` stays the
  default, so a run without `--executor` is unchanged. A run that opts in with
  `--executor opencode` before #1313 lands has its worker refused by serve and fails its
  nodes; that is why the default was not switched.

## Migration / rollback

- Existing `verdict orchestrate` runs with no `--executor` flag are unchanged: they use
  `direct-gateway`. To use OpenCode, pass `--executor opencode`.
- `prime-headless` users are unaffected.
- Making OpenCode the default is a later one-line change to the `DEFAULT_REGISTRY` default
  (`default=True` on `OpenCodeAdapter`, removed from `DirectGatewayAdapter`), with the
  default tests flipped, made only after a live smoke passes with #1313 in place. If the
  adapter is defective before then, nothing needs reverting for runs that do not name it.

## ADR impact

- Amends nothing yet: [ADR-041](../../../docs/adr/ADR-041-generic-harness-contracts.md)'s
  default worker executor (`direct-gateway`) is unchanged until the default is switched.
  This change adds an opt-in `opencode` worker executor, and ADR-053 Decision 2 is amended
  (2026-11-24): the move of the default to `opencode` waits for #1313 and a passing live
  smoke.
- Relates to [ADR-036](../../../docs/adr/ADR-036-goal-to-receipt-orchestration.md)
  (goal-to-receipt orchestration): worker output becomes richer (edit loop
  instead of answer-only).
- No impact on admission, ranking, or cost-policy ADRs.

## Security / trust implications

- **API key handling:** the bearer token for `verdict serve` must not appear in
  the OpenCode config file, in log output, or in process arguments. It is passed
  via an env var or a credential field that OpenCode does not echo; the builder
  confirms the mechanism.
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
- **Context:** the context pack is delivered as a file in the node worktree
  (`.verdict-context.md`). The OpenCode agent reads it via the `instructions`
  config field. No memory is shared between attempts.
- **Memory:** no Hindsight or other memory plane interaction in the worker path;
  memory is a controller concern, not an executor concern.
