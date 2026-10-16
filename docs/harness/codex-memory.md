# Codex memory hooks

How Verdict connects its memory plane to Codex CLI hooks (checked against codex-cli 0.161.0).

## Stdin hook model

Codex runs each hook command and passes the event as JSON on stdin. The payload includes
`session_id`, `turn_id`, `cwd`, `hook_event_name`, and `transcript_path`. Verdict hooks read
that payload directly. The legacy hooks used `/tmp/codex_last_prompt` and
`/tmp/codex_session_prompt` instead, and those files leaked between concurrent sessions and projects.

## Events and evidence levels

`verdict.harness_codex.certify()` reports a level for each event in `CertifyReport.hook_events`. An event
is `supported` only after an isolated live smoke run records it firing. That hasn't happened yet.

| Event | Level |
| --- | --- |
| SessionStart | partial |
| UserPromptSubmit | partial |
| PreToolUse | partial |
| PostToolUse | partial |
| PreCompact | partial |
| SubagentStart | partial |
| Stop | partial |
| PermissionRequest | unknown |
| PostCompact | unknown |
| SubagentStop | unknown |
| SessionEnd | unknown |

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
- Install removes legacy Verdict entries, including any `verdict hook ...` command that reads
  `/tmp/codex_*`, before writing the stdin hooks.
- `uninstall_codex_hooks(hooks_file)` removes only Verdict entries and drops any event list
  that ends up empty.

## Hook command form (pending)

`hook_command(event)` is the only place the command string is defined. It currently returns
`verdict hook codex-event <Event>`. The other option is `python -m verdict.memory_harness.codex <Event>`.
The controller hasn't chosen between them yet. Until it does, the CLI entry point isn't wired,
and nothing should claim the hooks run end to end.
