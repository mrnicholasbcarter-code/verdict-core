# Memory harness installer signatures

Stable function signatures for the per-harness memory installers. Lanes A–D
build against these entry points. `configure_memory_bridge` remains the
production caller and stays in `verdict.memory_bridge`.

`shared_db` on the Claude and Codex hook installers is accepted and unused.
Those hooks do not embed the plane path.

## Claude (lane A)

```text
verdict.memory_harness.claude.install_claude_hooks(settings_file: 'Path', shared_db: 'Path') -> 'None'
verdict.memory_harness.claude.install_claude_instructions(instructions_file: 'Path', bridge_instruction: 'str') -> 'None'
```

`install_claude_hooks` writes Claude Code `settings.json` hooks.
`install_claude_instructions` appends the bridge block to a project
`CLAUDE.md` when the marker `Verdict Unified Memory Bridge` is absent.

## Codex (lane B)

```text
verdict.memory_harness.codex.install_codex_hooks(hooks_file: 'Path', shared_db: 'Path') -> 'None'
verdict.memory_harness.codex.install_codex_instructions(codex_dir: 'Path', bridge_instruction: 'str') -> 'None'
```

`install_codex_hooks` writes Codex `hooks.json`. `install_codex_instructions`
creates the project `.codex` directory and appends the same bridge marker to
`AGENTS.md` once.

## Hermes (lane C)

```text
verdict.memory_harness.hermes.install_hermes_bridge(home: 'Path', shared_db: 'Path') -> 'None'
```

Writes `home / ".hermes" / "verdict_bridge.json"` only when `.hermes` already
exists, and overwrites that file. It does not create `.hermes`.
`configure_memory_bridge` still records `hermes` as configured when the
directory is missing.

## Prime (lane D)

```text
verdict.memory_harness.prime.install_prime_bridge(home: 'Path', shared_db: 'Path') -> 'None'
```

This tree has no Prime memory-bridge installer. The function writes nothing.
`configure_memory_bridge` calls it for the tool name `prime` and does not add
`prime` to `configured_tools`. Remove that call only when this function writes
Prime files and the characterization tests pin those files.

`pi` inside `configure_memory_bridge` is Perplexity Pi. It is not this module.

## Consumer entry points that stay in memory_bridge

```text
verdict.memory_bridge.configure_memory_bridge(selected_tools: 'list[str]', plane: 'MemoryPlane | None' = None, home_dir: 'Path | None' = None, cwd: 'Path | None' = None) -> 'dict[str, Any]'
verdict.memory_bridge.uninstall_memory_bridge(home_dir: 'Path', cwd: 'Path', purge_data: 'bool' = False) -> 'dict[str, Any]'
```

## Re-exports

None. Production callers import `configure_memory_bridge` and
`uninstall_memory_bridge` from `verdict.memory_bridge`. The old private names
`_install_claude_hooks` and `_install_codex_hooks` had no other importer.

Add a re-export only for a real importer of those private names, and delete
it when that importer imports `verdict.memory_harness` directly.

## Not part of this signature

Session import, MCP `.mcp.json`, Cursor rules, and the Pi config stay in
`configure_memory_bridge`. Uninstall stays there too: it can delete
`home / ".verdict"` when purge is requested, unlinks `cwd / ".mcp.json"`, and
only notes Claude settings without editing them.
