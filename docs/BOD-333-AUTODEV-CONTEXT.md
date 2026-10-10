# BOD-333 phase 1: autodev packet context output observations

`verdict autodev packet execute --context-output-policy` opts in for one run.
The flag is off by default. The Python packet launch API accepts
`output_policy=ContextOutputPolicy(enabled=True)`; both compiler seams accept it.
No shared configuration, HOME storage, provider calls, or tool interception is added.

Production path: `commands/dispatch.py` -> `cli.cmd_autodev_packet_execute` ->
`autodev_run.run_packet_autodev` -> `compile_packet_context` ->
`compile_worker_context` -> the existing `ContextPackCompiler`.
The action registry's `autodev.packet.execute` launch entry uses the same domain path.
`WorkerController._run` has no compiler seam: its prompt arrives pre-built.
Prime-owned tool loops are not intercepted; `harness_prime` reports
`tool_interception` and `tool_pre_post` as unsupported.

The policy observes the raw **post-compilation context string** placed in
`WorkUnit.context`. It runs no transform or compression engine. Raw passthrough
is the only supported transform, so no lossy candidate is forwarded. Compiler
budget, security exclusions, and sanitization remain unchanged. This flag does
not claim to preserve facts that the compiler already omitted or sanitized.
System constraints, code/edit arguments, paths, digests, refusals, and test counts
in the compiled context are not rewritten by the output policy.

The existing context receipt gets `raw_artifact_pointer` (SHA-256 and UTF-8 bytes),
`output_policy`, and `output_metrics`. This pointer identifies the context string,
not the entire provider request. PatchExecutor adds its own instructions and owned
source outside that string. Input/output bytes measure only raw policy passthrough;
`utf8_bytes_div_4` is an explicit estimate, never billed usage. Provider input/output
token counts remain `null` here. Unknown raw bytes and estimates remain `null`.
The pointer is an integrity reference, not a new raw-content storage API. Retrieve
raw text from the returned context pack; persisted compiled prompts stay redacted.

OFF preserves pack bytes, digest, receipt shape, and legacy receipt key. ON adds a
receipt-key suffix to avoid colliding with an OFF observation of the same pack.
Pack identity does not include these output observations. Phase 2 compression,
reuse/caching, provider cache metrics, and matched live acceptance are not shipped
by this phase-1 flag.
