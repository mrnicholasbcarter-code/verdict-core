# Claude Code memory hooks

How Verdict connects its memory plane to Claude Code hooks (checked against Claude Code 2.1.300).
This covers the memory side of the Claude adapter. Provider switching and the SessionStart
launch gate are covered in [Harness adapters](../guides/harnesses.md#claude-code).

## What it does

- **Capture.** Verdict installs one hook per Claude Code lifecycle event. Claude Code passes
  the event to the hook as JSON on stdin (`session_id`, `cwd`, `hook_event_name`, and
  `prompt` on `UserPromptSubmit`). The hook turns that payload into a `lifecycle-event/v1`
  record and appends it to a local outbox. The legacy hooks read `/tmp/claude_*` prompt
  files instead, and those files leaked between concurrent sessions and projects. The new
  hooks never read `/tmp`.
- **Recall.** The installer appends the Verdict Unified Memory Bridge block to the project
  `CLAUDE.md`, once. The block tells the agent to query the memory plane with
  `verdict memory search '<query>'` before a task and to export records with
  `verdict memory put` when it finishes.

## Hook translation

`verdict.harness_claude.translate_hook_payload(payload, claude_version=...)` is a pure
function: it reads nothing but the payload.

| Claude Code event | lifecycle kind |
| --- | --- |
| SessionStart | `session_start` |
| UserPromptSubmit | `before_first_model_turn` |
| PreToolUse | `before_tool` |
| PostToolUse | `after_tool` |
| SubagentStop | `subagent_end` |
| PreCompact | `before_compact` |
| Stop | `before_yield` |
| SessionEnd | `session_end` |

- `project` and `scope` are the basename of `cwd`. Two sessions in different projects never
  share a session or a project.
- `content` is the prompt for `UserPromptSubmit` and a short event label otherwise.
- `source_event_id` is a sha256 of the payload serialized as canonical JSON. The same payload
  always gives the same id, so a replayed hook is recognized and not written twice.
- A payload that isn't valid JSON, is missing `session_id`, `cwd` or `hook_event_name`, or names
  an unknown event is rejected.

## Hook entrypoint

Each installed hook runs `python -m verdict.memory_harness.claude <Event>`
(`verdict.harness_claude.HOOK_COMMAND_TEMPLATE`). The entrypoint:

- reads the payload from stdin and writes nothing to stdout, so nothing is injected into the
  model's context;
- appends the record to `$VERDICT_CLAUDE_OUTBOX_DIR/claude-lifecycle.jsonl` (default
  `~/.verdict/outbox/claude`). The outbox is append-only and deduplicated by `source_event_id`;
- exits 0 on success or replay;
- exits 1 with a reason on stderr for a malformed payload, an argument that doesn't match the
  payload's `hook_event_name`, or an outbox it can't write. Claude Code treats exit 1 as a
  non-blocking error: it shows the reason and carries on.
- never exits 2. In Claude Code, exit 2 blocks a tool call, erases a prompt or prevents
  stopping, and a capture hook must never block the user's turn. The fail-closed launch gate is
  a separate SessionStart hook (`verdict hook claude-gate`) and keeps its own semantics.

## Install and uninstall

```bash
verdict hook configure --tools claude   # install the hooks and the CLAUDE.md block
verdict uninstall                        # remove Verdict hooks and MCP registrations
```

These call `install_claude_hooks`, `install_claude_instructions` and `uninstall_claude_hooks` in
`verdict.memory_harness.claude` (signatures in
[Memory harness installer signatures](../guides/memory-harness-installer-signatures.md)).

- Install is idempotent: running it twice leaves `~/.claude/settings.json` byte-identical.
- User hooks and every other settings key are kept.
- Install removes legacy Verdict entries first, including any `verdict hook ...` command that
  reads `/tmp/claude_*` and the provisional `python -m verdict.harness_claude hook` form.
- Uninstall removes only Verdict-managed entries and drops event lists left empty. After
  install then uninstall, the settings file matches the original byte for byte. If there is no
  settings file, uninstall does nothing and creates nothing.

## Status and certify

`verdict harness claude certify` reports a level for each hook event in
`CertifyReport.hook_events` and adds a note listing the events observed live. An event is
`supported` only after an isolated live run records it firing.

| Event | Level |
| --- | --- |
| SessionStart | supported |
| UserPromptSubmit | supported |
| PreToolUse | supported |
| PostToolUse | supported |
| Stop | supported |
| SessionEnd | supported |
| SubagentStop | partial |
| PreCompact | partial |

- `supported`: observed firing in Claude Code 2.1.300 on 2026-10-16.
- `partial`: installed and translated, but not yet observed live.

The adapter's overall level stays `partial`, because Claude Code's default Anthropic Messages
traffic is not proxied by Verdict yet.

## Isolated-HOME proof

No test or smoke run touches the operator's own Claude configuration.

- Tests run the installer against a temporary `HOME` and settings file, and run the
  entrypoint as a real `python -m` subprocess with `VERDICT_CLAUDE_OUTBOX_DIR` pointed at a
  temporary directory.
- The live runs used two sessions in separate projects, each with its own `HOME` and
  `CLAUDE_CONFIG_DIR`. The real hook payloads are kept, anonymized, in
  `tests/fixtures/claude/v2_1_300/` (SessionStart, UserPromptSubmit, SessionEnd) and
  `tests/fixtures/claude/v2_1_300_omniroute/` (all six supported events, with their original
  sha256 values). The tests replay those payloads through the translator and the entrypoint
  and check that the live set is complete and anonymized.

## Known limits

- **Live proof on a Claude model is pending.** The 2.1.300 run used a non-Claude backend model
  through the gateway, because the Claude routes were out of quota. It proves Claude Code's hook
  surface and payload shapes, not a Claude model's behavior. A rerun on a Claude model is
  blocked until the weekly limit resets on 2026-10-18 at 7 PM ET.
- **The hook command uses a bare `python`.** On pipx or uv installs, the system `python` may not
  be able to import `verdict`. A change that pins the hook to the absolute interpreter that ran
  the install is pending review.
- **No backend delivery yet.** Records stay in the local outbox.
- **The provider round-trip test is skipped** until the `verdict_memory` package with
  `FakeMemoryProvider` is available.
