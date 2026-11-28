# ADR-054: Workers are driven through the Agent Client Protocol with one executor

**Status:** Proposed. The direction is the operator's decision of 2026-11-28 (#1421). The text waits for the controller's approval under the operator's delegation. Nothing in it is built yet.
**Date:** 2026-11-28
**Deciders:** Operator (direction); controller (text)
**Amends:** [ADR-053](ADR-053-opencode-worker-adapter.md). It adds a second way to drive a worker and, later and in its own change, replaces the default of Decision 2. Decisions 1, 4 and 6 of ADR-053 stay as they are (Decision 3 below).
**Relates to:**
[ADR-041](ADR-041-generic-harness-contracts.md) (generic harness contracts),
[ADR-049](ADR-049-cost-policy-and-unknown-handling.md) (cost policy and unknown handling)

## Context

ADR-053 drives OpenCode with `opencode run --standalone --auto` and reads the newline-delimited JSON it prints. That code is tied to one tool's flags and to one version's output shape (`_parse_ndjson_output` says it is the shape OpenCode 2.3.2 prints). The adapter is pinned at 2.3.2. On 2026-11-28 the installed CLI was 2.4.2 (#1421). A second agent CLI is installed (#1421 describes it as a fork of the older 1.x line of the same tool). Under ADR-053 it would need its own adapter and its own parser.

Both installed CLIs have an `acp` command that speaks the Agent Client Protocol: JSON-RPC over stdio. The operator decided on 2026-11-28 to target that protocol instead of one adapter per tool (#1421).

**Evidence.** Every fact below comes from the spike's findings comment on #1422 (2026-11-28, "the spike") or from the code on main. The spike's recordings are in a scratch directory, not in this repository. The spike drove OpenCode 2.4.2 and, as a stretch run, Kilo 7.8.40 through `acp`. The client, a fake model endpoint and the agent ran in a network namespace with loopback only, so no model request and no real gateway were involved. The per-attempt config and environment came from the executor's own functions. Where the spike did not show something, this ADR says so (section "Not yet shown").

## Decision

**1. One ACP executor. No per-tool adapter.**
Verdict gets one executor that drives a worker over ACP. Agents differ only by a profile (Decision 9). The spike found no blocker: both agents answered `initialize` with protocol version 1, took a session and a prompt, and reported tool calls, a stop reason and cancel in the same way.

**2. What the protocol carries, and what stays outside it.**

The protocol carries (spike):

- Start: `initialize` returns the protocol version, `agentInfo` (name and version) and the agent's capabilities. `session/new` takes the working directory and the MCP server list and returns a session id and the config options (model, effort, mode).
- The task: `session/prompt` with a text block. It needs no terminal and no `--auto` flag.
- Progress: `session/update` notifications. They carry `tool_call` and `tool_call_update` (kind, locations, `rawInput`, status), `agent_message_chunk` and `usage_update`. Some are noise (`available_commands_update`).
- Permission: the agent sends `session/request_permission` and the client answers with one of the options the agent offered, each with a kind (`allow_once`, `allow_always`, `reject_once`).
- Cancel: the client notification `session/cancel`; the prompt then ends with the stop reason `cancelled` (0.04 s in the spike).
- The end: a stop reason on the prompt response, with `usage` when the agent gives it. Version: `agentInfo.version` from `initialize`.

These stay outside the protocol:

| Item | Where it lives (spike) |
|---|---|
| Per-attempt config isolation | The agent's config files and environment. `initialize` and `session/new` carry only the working directory and the MCP servers. |
| The provider pointed at `verdict serve`, and the signed assignment as its credential | The agent's config (ADR-053 Decision 1). The client selects the model with `session/set_config_option` (`configId` `model`, value `verdict/<route>`). |
| The served model | `verdict serve`. The model string appears only in the session's config options, which show what was selected. No response carries it. |
| HTTP status and retry-after | OpenCode sends them in an extension, `_meta."opencode/retry"` in a `session_info_update`. The final JSON-RPC error has neither. Kilo sent nothing about them. Serve's receipts record them. |
| Exit status | Nothing. A stop reason or a JSON-RPC error takes its place. The agent exited 0 after its stdin closed, which says nothing about the turn. |
| A timeout | Nothing. See Decision 7. |

**3. What stays exactly as ADR-053 has it.**
The ACP executor keeps these parts of ADR-053 unchanged, because the protocol does not carry them:

- The per-attempt config directory and environment, written by the same code as today (`_write_attempt_config`, `_write_task_file`, `_build_child_env`).
- The provider `verdict` pointed at `verdict serve`, with the other providers denied, the title agent pinned to the same route, and the signed per-attempt assignment as the worker's only credential (Decision 1 and its notes).
- The `external_directory` deny, and shell commands not restricted (Decision 4, the operator's choice).
- The served model is verified by `verdict serve`, from its receipts, as today. The protocol is never the proof of it.
- Token counts are advisory. They come from `usage` on the prompt response and from `usage_update`, are recorded as the agent's own count, and stay `null` with a reason when the agent gave none (it gives none on cancel). Serve's receipts stay authoritative. In the spike OpenCode summed the requests of the turn and left out the title request, and Kilo reported the last request only. So serve shows one more request per session than the agent's usage covers. The agent's `cost` field is not recorded as a cost: the spike saw `0 USD`, and cost comes from declared or observed facts (ADR-049).

**4. The client: standard library, synchronous, recorded, no new dependency.**
Verdict writes a small ACP client (#1425). It uses the Python standard library, is synchronous (a blocking reader thread and a deadline-bound call, as in the spike), and adds no dependency.

- Every message in both directions is recorded, with secrets removed by the existing scrubber.
- Messages are handled as plain dictionaries. A message that is malformed, or a prompt result without a stop reason, is a failure, never a success.
- The surface is small. The spike client used `initialize`, `session/new` and `session/prompt`, the notification `session/cancel`, and the one agent-to-client request `session/request_permission`. The product adds `session/set_config_option`, which the spike tested separately on both agents: a valid value echoes the options and an unknown one returns -32602 "model not found". The spike client was 163 lines, about 35 of them recording and redaction.

The PyPI package `agent-client-protocol` is not used. The spike did not install or run it, so this is a judgement from its published description, not a test. The reasons are:

- The fields Verdict needs sit outside a typed core: `_meta."opencode/retry"`, the `usage` field (an unstable schema in the spike's words) and each agent's own stop behaviour. Verdict wants raw dictionaries, its own recording and redaction, and parsing that fails closed.
- The surface is small: three requests, one notification and one agent-to-client request in the spike, covered by a client of about 160 lines.
- It would be a new dependency (it needs pydantic). AGENTS.md says no dependency is added silently.

The package is asyncio, and the worker executors are asynchronous too (`WorkerExecutor.run` is a coroutine), so that is not a reason against it. A synchronous client inside an asynchronous executor must run off the event loop in a bounded helper thread, as the executor's readiness probes already do for their blocking HTTP call. This is a cost of the choice (Consequences). Revisit the choice if `fs/*`, `terminal/*` or session resume are adopted.

**5. Permission requests are answered by tool kind. A path rule cannot contain a shell command.**
The client answers a `session/request_permission` by the `kind` of the option it picks (`allow_once` or `reject_once`), never by option id or name. It never picks `allow_always`, so each request is decided alone. The rule is by the tool call's `kind`:

- `execute` (the shell): no path rule is applied. See the limit below. The client allows it, as ADR-053 Decision 4 decided, and records the command (`rawInput.command`).
- Every other kind: allow only when the request names at least one path (from `locations` and `rawInput`) and every path, resolved with `realpath`, is inside the node's worktree. Otherwise reject. A request with no path is rejected.

**The limit, stated plainly.** Containment of a shell command cannot come from a path rule on permission requests. In the spike:

- under the executor's default config, a shell command that wrote a file outside the repository ran with no permission request at all, and the file was written;
- under a config that asks for everything, the request's `locations` held only the working directory and the command was in `rawInput.command`; the path rule allowed it and the file outside the repository was written.

So containment of the shell stays with the agent's own permission config and the worktree. This is the risk ADR-053 Decision 4 already accepted (an unrestricted shell, owned files checked afterwards). The spike makes it concrete. It does not add a new one, and the ACP executor does not claim to remove it.

What the rule does cover is the edit tools. Under the executor's config an edit outside the worktree got no permission request: the agent's `external_directory` deny returned a failed tool update ("Permission denied: external_directory"), and the file was not created. When the same write was asked (config `ask`), the client's rejection stopped it. The client's rule is the second line behind that config.

**6. Stop reason and JSON-RPC error map to Verdict's failure classes.**
The executor turns the outcome into the existing `WorkerTerminal` fields, and the existing `RecoveryPolicy.classify` names the class. Verdict keeps its own record of what it sent: the spike showed that a stop reason alone cannot say who ended the turn.

| What arrives | What is recorded | Class |
|---|---|---|
| `end_turn`, Verdict sent no cancel and refused no request, and the agent's messages carry text | `ok`, stop reason `end_turn`, `output` is the text of the `agent_message_chunk` updates, usage when given | Success; verification and the gates run as today |
| `end_turn` under the same conditions, but no message text | `ok`, empty `output` | `empty_output`: the existing rule fails an empty output even when `ok`, as it does for the existing executor today. This ADR does not change that rule |
| `cancelled` after Verdict sent `session/cancel` because its own timer ran out | stop reason `timeout` | `timeout` |
| A stop reason after Verdict refused a permission request (the spike: `cancelled` on OpenCode, `end_turn` on Kilo) | `worker_blocked:` with the refused kind and path | `worker_blocked`: task feedback, no cooldown, the node is corrected, no provider is blamed |
| `cancelled` with no cancel sent and no refusal | not ok, the stop reason as given | `no_final_answer`, by the existing rule for any stop reason other than `stop` or `end_turn` |
| Any other stop reason (the spike saw only `end_turn` and `cancelled`) | not ok, the stop reason as given | `no_final_answer`, the same rule |
| JSON-RPC error on `session/prompt` (the spike: -32603, `data.errorName` `provider.internal`, after the agent's own retries) | the error text; the status and retry-after of the last retry notice when the profile reads one (OpenCode: 503, 7 s) | With a status, the existing status rules (503 is `upstream_temporary`). Without one (Kilo, or no notice), the text rules, otherwise `unknown` |
| JSON-RPC error on `session/set_config_option` (the spike: -32602 "model not found"), or on `initialize` or `session/new` | `harness_configuration:` with the error | `harness_configuration`: blocked, no cooldown, nothing ran |
| `initialize` reports a protocol version other than 1 | `harness_configuration:` | `harness_configuration` |
| A result without a stop reason, or a malformed message where a result belongs | `malformed ACP result` | `malformed_response` |
| The agent exits or its pipe closes before the prompt result | `worker_error:` and the exception or exit kind, never the message text | `unknown` |

The last three rows are decisions to fail closed. The spike did not exercise them. A JSON-RPC error on `session/prompt` is expected to arrive after the agent's own retries (Decision 7), so its text can be minutes old.

**7. Verdict keeps its own turn timeout: cancel, then kill.**
Timeout is not an ACP concept. The agent retries a 503 on its own: in the spike OpenCode sent 11 requests (the first and 10 retries) 7 to 11 s apart, about 91 s in all, before the JSON-RPC error. Only a cancel stops that. So the executor keeps the limits of ADR-053 Decision 6, the attempt timeout (default and maximum 900 s) and the idle bound (`VERDICT_WORKER_IDLE_SECONDS`, 300 s by default). When the timer runs out the executor sends `session/cancel` and waits a bounded time for the stop reason. If none comes, it closes stdin and then kills the attempt's process group by the marker the existing executor uses. In the spike a cancel ended the turn in 0.04 s on OpenCode (0.05 s on Kilo) and the agent dropped its HTTP call; kill was never needed. Closing stdin made OpenCode exit 0 in 0.03 s and Kilo in 2.1 s. OpenCode starts one helper child (`serve --stdio --port 0`) that left with its parent. The wait bound is set in the client slice.

**8. Environment variables that stop the agent from fetching model lists, and from updating itself.**
The ACP executor adds these to the child environment:

- `OPENCODE_DISABLE_MODELS_FETCH=1` and `OPENCODE_DISABLE_FREE_MODELS=1`. In the spike, with neither set, the agent resolved `models.opencode.ai` at start; with both set it made no DNS lookup. They were tested together only, so this ADR does not say which one does which.
- `OPENCODE_DISABLE_AUTOUPDATE=1`. The spike took the name from the binary and ran with it set. It did not test whether the agent would have updated without it; the binary was unchanged after every run.

The existing executor sets none of the three. Whether to add them there is not decided here. Two things no variable stopped: the agent always probes the loopback ports 1234, 11434 and 8000 for local providers, and the executor passes the real HOME through its allowlist (the spike used a scratch HOME).

**9. Agent profiles. No per-tool adapter is added.**
A profile holds only what differs between agents:

- the command that starts ACP mode (`<binary> acp`);
- the writer of the per-attempt config, in the shape that agent reads (for the first agent, the code ADR-053 already has);
- the environment additions (Decision 8 and the isolation variables);
- readers for an agent's own extensions, such as OpenCode's retry notice;
- the agent version its live check passed on.

A profile holds no parser, no permission rule, no failure map and no timer. Those belong to the executor, once.

The first profile is OpenCode, the CLI Verdict already uses. The second is the fork, Kilo, which has its own config shape. In the spike it worked as a profile: protocol 1, a different config shape, and no version-pin helper. Differences seen: its usage is the last request only, its permission request for an outside write (config `ask`) used kind `other` with the file and its directory as locations, it ended `end_turn` after a rejected request, and it sent nothing about its 503 retries. The version the agent reports in `initialize` is recorded on every attempt. A version that differs from the profile's is a `verdict doctor` warning, as in ADR-053 Decision 5.

**10. The existing executor stays the default until the ACP executor passes the live check.**
The ACP executor is added beside the existing one and is chosen by an option. The default does not change in the change that adds it. The default changes only after the adapter's live check has passed through the ACP executor, and the switch is its own change, as ADR-053 did for its own default. The live check is ADR-053's, run through the ACP executor: one attempt through `verdict serve` on a pinned route; the edit lands and its verification passes; serve's receipts show the pinned route and the requests under the signed assignment. The epic's final check (#1421) adds a `verdict orchestrate` run that ends COMPLETE with the served model verified by serve, token counts in the receipt, and a denied write outside the worktree. Retiring the old path is a later change again.

## Consequences

- Another agent that speaks ACP is a profile, not new code. A new CLI version no longer breaks an output parser. A version past the profile's pin is a recorded fact and a warning.
- Silent config denials. A deny in the agent's config is not a permission request. The client sees only a failed tool update with free text ("Permission denied: external_directory") and the turn goes on. The executor records every failed tool update, but cannot tell a deny from any other failure except by that text.
- Different stop reasons after a refused permission. OpenCode ends the turn with `cancelled`, which looks like a client cancel. Kilo ends with `end_turn`. The executor must keep its own record of what it sent and refused (Decision 6); the stop reason alone is not evidence.
- The agent's own 503 retry loop (about 91 s on OpenCode) runs inside one turn. Verdict's timer bounds it. The retry notice that carries the status and retry-after is an OpenCode extension, and Kilo has none, so its failure class there falls back to text rules or `unknown`.
- The shell is not contained by the client. See the limit in Decision 5.
- A synchronous client under an asynchronous executor needs a bounded helper thread. On timeout the executor cancels and kills the process group, which closes the pipe the thread reads. The thread must end on every exit path (normal, timeout, cancel, exception). This belongs to the client and executor slices and needs tests.
- Two executors exist until the old one is retired. Both must keep passing their own live check.
- Serve shows one request more per session than the agent's usage covers (the title request).

## Not yet shown

The spike did not exercise these. Each stays open until a slice shows it:

- A run through the real `verdict serve`. The spike used a fake model endpoint. A serve denial of the assigned route was not seen. The retry notice has the same fields (`error.message`, `error.response.body`) that the existing early-stop check reads in the CLI's retry events (`opencode_denial.py`). Whether serve's denial text arrives there over ACP is not shown.
- HTTP 401 and 429 responses.
- Auto-update enabled. The binary was unchanged with it off.
- The second agent's final error after a 503 retry loop. Kilo was watched for 25 s and then cancelled.
- Whether the operator's global agent config merges under `OPENCODE_CONFIG_DIR` when the real HOME is passed.
- A `question` tool call that stalls a turn.
- What the agent's loopback probes send, and whether any reach `verdict serve` on port 8000.
- Advertising `fs/*` to the agent, which might give the client a path check on writes. Session resume and fork, which the agent offers.
- A protocol version other than 1, an error on `initialize`, and an agent that dies mid-turn (the last three rows of Decision 6).

## Build slices

The client module is #1425. The executor, the live check through `verdict serve` and the default switch follow in their own changes under #1421, each with its own OpenSpec change.
