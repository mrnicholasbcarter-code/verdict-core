# Execution-host contract

Verdict's execution-host boundary is defined by
`verdict.execution_hosts`. It describes what an adapter may detect, preview,
invoke, and cancel; it does not itself probe the machine or launch a process.

Before execution, an adapter produces an `ExecutionPreview` containing the
selected host, provider/model, repository/worktree, permissions, hard budget,
fan-out, lifecycle state, and a digest of the objective. Raw objectives,
credentials, and argv values are not part of the preview. The preview is
therefore suitable for consent and evidence review.

Adapters must declare operations through `HostCapabilities`. Detection is
adapter-owned and bounded; Verdict does not assume that a universal
`--version` probe is safe. Invocation and cancellation are separate declared
capabilities, and a result is accepted only when its lifecycle and termination
reason agree on a terminal outcome.

This is the initial #108 contract slice. Concrete Codex, Claude Code, and Pi
adapters remain separate implementations behind this boundary and must add
their own detection, process, cancellation, truncation, and failure tests.
