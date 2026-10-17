# Codex memory hooks

How Verdict connects its memory plane to Codex CLI hooks (checked against codex-cli 0.161.0).

## Stdin hook model

Codex runs each hook command and passes the event as JSON on stdin. The payload includes
`session_id`, `turn_id`, `cwd`, `hook_event_name`, and `transcript_path`. Verdict hooks read
that payload directly. The legacy hooks used `/tmp/codex_last_prompt` and
`/tmp/codex_session_prompt` instead, and those files leaked between concurrent sessions and projects.

## Events and evidence levels

`verdict.harness_codex.certify()` reports a level for each event in `CertifyReport.hook_events`. An event
is `supported` only after an isolated live smoke run records it firing; those payloads are kept, redacted,
as `tests/fixtures/codex/v0_161_0/live_*.json`.

| Event | Level |
| --- | --- |
| SessionStart | supported |
| UserPromptSubmit | supported |
| PreToolUse | supported |
| PostToolUse | supported |
| PreCompact | partial |
| SubagentStart | partial |
| Stop | supported |
| PermissionRequest | unknown |
| PostCompact | unknown |
| SubagentStop | unknown |
| SessionEnd | supported |

- `supported`: observed live on 2026-10-16 (isolated smokes; Stop, PreToolUse and PostToolUse came from completed OmniRoute turns on a free model).
- `partial`: the event name appears in the 0.161.0 binary, and a working hooks.json on the same host uses it.
- `unknown`: only the name is known.

The `hooks` facet is `partial`, not `unsupported`.

## Provenance

`verdict.memory_harness.codex.event_provenance(payload)` turns one payload into
`session_id`, `turn_id`, `cwd`, `project`, `event`, `transcript_path`, and `event_hash`.

- `project` is the nearest ancestor of `cwd` that contains `.git` or a `.codex/` directory.
- `event_hash` is a sha256 of the payload, serialized as JSON with sorted keys. Keys starting
  with `_` are left out. The same payload always gives the same hash, and any change to a
  material field, such as `turn_id`, gives a different one.

Two sessions in different projects never share a session, project, or hash. Verdict never reads `/tmp` here.

## Install, uninstall, upgrade

- `install_codex_hooks(hooks_file, shared_db)` registers one Verdict command for each of
  the 11 events. Running it again doesn't change the file. User hooks and other top-level
  keys are kept. The file is written atomically.
- Install removes legacy Verdict entries before writing the stdin hooks: earlier
  `<python> -m verdict.memory_harness.codex <Event>` hooks under any interpreter, and a direct
  `verdict hook record|recall` command whose prompt argument reads `/tmp/codex_last_prompt` or
  `/tmp/codex_session_prompt`.
- A hook counts as Verdict's only when `shlex.split` parses it as exactly
  `<python> -m verdict.memory_harness.codex <Event>` with a known Codex event, where `<python>` is
  the pinned `sys.executable` or a bare, relative or absolute `python`, `python3` or
  `python3.N`. Wrappers such as `sh -c '...'` or `logger -- '...'`, extra arguments, other
  modules and unparseable commands are user hooks and are never removed, even when they
  mention the module (`verdict/memory_harness/_hook_ownership.py`).
- `uninstall_codex_hooks(hooks_file)` removes only Verdict entries and drops any event list
  that ends up empty.

## Hook entrypoint

`hook_command(event)` is the only place the command string is defined. It returns
`<python> -m verdict.memory_harness.codex <Event>`, the form the controller chose, where
`<python>` is the absolute, shell-quoted `sys.executable` that ran the install. A bare `python`
on PATH would miss verdict on pipx and uv installs. Reinstalling replaces Verdict hooks written
by any interpreter, including the older bare-`python` form (see the ownership rule above). The
entrypoint:

- reads the payload from stdin and writes nothing to stdout;
- runs the launch gate first. An unknown event, a `hook_event_name` that doesn't match the
  event argument, a missing `session_id` or `cwd`, or a payload that isn't valid JSON fails
  closed with exit code 2 and a reason on stderr;
- otherwise appends the provenance record to the outbox at
  `$VERDICT_CODEX_OUTBOX_DIR/codex-lifecycle.jsonl` (default `~/.verdict/outbox/codex`),
  deduplicated by `event_hash`, and exits 0. There is no backend client yet, so every record
  carries the named gap `no-backend`. This is the one place it degrades gracefully;
- exits 1 with a reason on stderr if the outbox write fails.
