# Global runtime ownership runbook

Verdict treats Ruflo/claude-flow and RuVector as optional services. The
canonical identities and endpoints are defined in [ADR-008](../adr/ADR-008-global-runtime-ownership.md).

Inspect without changing processes:

```bash
verdict runtime status --json
verdict runtime reconcile --plan --json
```

Both commands are read-only. `ambiguous-process-identity` and `port-collision`
are operator review states, not permission to stop a process.

To apply only already-proven duplicate stops, review the plan and provide both
an exact service scope and explicit consent:

```bash
verdict runtime reconcile --apply --yes --service ruflo-mcp --json
```

The command re-reads `/proc/<pid>` and verifies UID, process start time, and
command-line hash before sending `SIGTERM`. It never uses `pkill`, process-name
only matching, workspace deletion, or an implicit restart. If a PID was reused,
the command fails closed.

The runtime state directory is `~/.verdict/runtime` by default and may be
overridden for an isolated test with `VERDICT_RUNTIME_STATE_DIR`. Launcher
commands are opt-in through the service-specific `VERDICT_*_COMMAND`
environment variables; Verdict does not invent or silently start a provider
service. State files are mode `0700` for the directory and `0600` for ownership,
PID, and lock files.

Recovery for the current host topology is deliberately staged:

1. capture `runtime status --json` and preserve it as redacted operator evidence;
2. inspect service command lines, systemd user units, endpoint health, and the
   ownership records;
3. establish or repair one canonical ownership record through the documented
   service supervisor;
4. rerun the plan and apply only exact duplicate actions after human review;
5. rerun status and the MCP initialize/tool handshake independently.

No database, credential, authorization header, prompt, or raw MCP payload is
part of the runtime report.
