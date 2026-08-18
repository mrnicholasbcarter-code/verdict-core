# ADR-008: Explicit Global Runtime Ownership

- **Status:** proposed for issue #129
- **Date:** 2026-07-29
- **Deciders:** Verdict Core maintainers
- **Related:** #108, #110, #126, #129

## Context

Ruflo/claude-flow and RuVector MCP processes can be launched by multiple
workspaces, MCP clients, and supervisors. Process-name matching and broad
signals are unsafe: PID reuse, command drift, and unrelated Node/MCP servers
must not be treated as owned services. Verdict also must remain usable when
these optional services are unavailable.

## Decision

Verdict defines one versioned global ownership contract for the following
service identities:

| Service | Endpoint | State root |
| --- | --- | --- |
| Ruflo/claude-flow daemon | documented by its launcher | `~/.claude-flow` |
| Ruflo MCP bridge | `http://127.0.0.1:20133/mcp` (`/healthz`) | `~/.claude-flow` |
| RuVector MCP bridge | `http://127.0.0.1:20130/mcp` (`/healthz`) | `~/.ruvector` |

Ownership records live below `~/.verdict/runtime` and contain only the service
ID, PID, UID, process start time, command-line hash, endpoint, creation time,
and contract version. They do not contain environments, credentials, prompts,
or MCP payloads. PID files are convenience metadata; the ownership record and
current process identity are authoritative.

`verdict runtime status --json` and `verdict runtime reconcile --plan` are
read-only. A process is a safe duplicate candidate only when its exact
contract markers, UID, workspace/state-root identity, and process identity
match the recorded canonical owner. Apply requires an explicit service scope
and `--yes`, revalidates PID start time and command hash immediately before a
bounded graceful `SIGTERM`, and refuses ambiguous or unowned matches. No
process-name-only matching, broad kill, silent restart, or hidden daemon spawn
is permitted. The runtime report intentionally treats an absent ownership
record as unavailable rather than adopting an existing host process.

## Consequences

- Runtime status remains useful offline and reports unavailable, ambiguous,
  and port-collision states explicitly.
- Concurrent starts use per-service restrictive lock files and idempotent
  ownership records.
- Existing host services are not implicitly adopted or stopped; an operator
  must first establish ownership through the explicit start/registration path.
- A future platform-specific supervisor may implement launch/restart, but it
  must preserve these identity and consent boundaries.

## Validation

`tests/test_runtime_daemons.py` covers read-only status, deterministic plans,
stale/PID-reuse and command mismatch, consent, revalidation, lock contention,
port collision, unrelated processes, and private versioned ownership records.
