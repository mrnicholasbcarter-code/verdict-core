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
`direct-gateway` before this ADR. ADR-053 makes `opencode` the new default,
which is the second amendment to ADR-041's initial default.

## Goals / Non-Goals

**Goals:**

- Register `OpenCodeAdapter` in `DEFAULT_REGISTRY` as the new default worker
  executor for `verdict orchestrate`.
- Build `OpenCodeExecutor` that runs `opencode run --auto --standalone
  --format json --model verdict/<route_id>` with per-attempt isolated config
  (`OPENCODE_CONFIG` → temp directory).
- Route all worker model calls through `verdict serve` (:8000); never direct to
  OmniRoute (:20128). Add per-request route pinning to serve if not present.
- Declare all nine `HarnessCapability` facets explicitly (ADR-041 requirement).
- Add OpenCode as the model-tier coding executor in `verdict autodev`.
- Pin OpenCode at v2.3.2; add a `verdict doctor` version warning.

**Non-Goals:**

- Interactive OpenCode sessions (outside scope of this story).
- MCP server use inside the worker (disabled by `--standalone`).
- Replacing PatchExecutor for mechanical autodev units.
- Changing the planner executor (`--planner-executor` stays `direct-gateway`).
- Full shell command allow-listing inside OpenCode (operator's explicit choice
  not to restrict shell; see Decision 4 / ADR-053).

## Decisions

The eight operator decisions (D36 / ADR-053) govern this change. Implementation
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
OpenCode config to a temp directory, sets `OPENCODE_CONFIG` for that process,
and deletes the temp directory on exit. This prevents cross-attempt and
cross-worker session DB contamination. The config contains:

- Provider entry: `verdict`, base URL `http://127.0.0.1:8000/v1`, API key from
  the serve bearer token (never written to disk or logs).
- Model entry: `verdict/<route_id>` mapped to `<route_id>`.
- Agent config: `permissions` restricting file edits to the node worktree;
  `external_directory` denied; shell commands not restricted (Decision 4).
- Instructions: path to `.verdict-context.md` in the worktree (context pack).

No secrets appear in the config file or in logs. The API key is passed as an
environment variable or via the config's credential field; the builder confirms
the exact mechanism during implementation.

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

**Context pack delivery (design detail):** The context pack and owned-files list
are written to `.verdict-context.md` in the node worktree before the attempt
starts, and the OpenCode instructions list references that file. The context
pack is Verdict's standard format; the builder confirms the exact injection
mechanism.

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
- **Per-request route pinning in `verdict serve`** may require non-trivial relay
  changes. If serve's current relay uses `allow_client_model_override` to pass
  the route, the builder must verify that model identity is correctly attested in
  the receipt and cannot be substituted by the worker.
- **JSON output field names** from `opencode --format json` are confirmed at
  build time. If usage fields are absent, `opencode session export <attempt_id>`
  is used as a fallback; the builder handles the case where both sources are
  incomplete.
- **`direct-gateway` users** who relied on the implicit default will get
  `opencode` after this merges. This is a breaking change for those users; the
  proposal documents it. Passing `--executor direct-gateway` explicitly is the
  migration path.

## Migration / rollback

- Existing `verdict orchestrate` runs with no `--executor` flag switch from
  `direct-gateway` to `opencode`. To keep `direct-gateway`, pass
  `--executor direct-gateway` explicitly.
- `prime-headless` users are unaffected.
- If the OpenCode adapter is defective, reverting the `DEFAULT_REGISTRY` default
  back to `direct-gateway` restores prior behaviour without any other changes.

## ADR impact

- Amends [ADR-041](../../../docs/adr/ADR-041-generic-harness-contracts.md) default
  worker executor (from `prime-headless`, later `direct-gateway`, to `opencode`).
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
