# Autonomous development workflow

This guide describes an evidence-driven way to change Verdict. A change is not
complete until its code, documentation, validation, review, and pull-request
state agree.

> **Current orchestration:** For goal-to-receipt execution, use
> [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) and the
> [interview golden path](interview-golden-path.md).

## 1. Establish context before editing

1. Read `AGENTS.md`, the relevant ADRs, specifications, issue acceptance
   criteria, and the affected code and tests.
2. Check the repository and worktree state. Preserve changes that you do not
   own.
3. Verify unfamiliar APIs and commands from their source or first-party
   documentation. Do not rely on remembered command shapes.
4. Write down the assumptions, limitations, affected paths, and validation
   commands for the change.

A timeout or malformed response from an optional documentation or MCP service
is unknown evidence. It is not proof that the service or a worker is ready.
Record the limitation and use a bounded, documented retry only for a transient
transport failure.

## 2. Bound the work

Give each implementation slice an owner, an explicit file boundary, acceptance
criteria, checks, and a reviewer. Use separate worktrees or disjoint ownership
for concurrent changes. Do not revert another worker's changes.

For non-trivial changes, inspect the callers, callees, imports, and relevant
tests before editing. The repository graph tools can help find that surface,
but they do not replace source review or tests.

## 3. Use the current execution path

ADR-036 owns a run from a high-level goal to a durable receipt. `verdict
orchestrate` obtains live gateway inventory, applies its eligibility ladder,
plans a validated work graph, runs ready nodes up to `--max-parallel`, verifies
nodes and integration, performs review unless `--no-review` is supplied, and
writes run artifacts. `--no-review` deliberately leaves the run blocked.

The orchestration executor launches one exact, selected route through the
Prime headless CLI. It does not let a worker choose a replacement model.
`verdict.subagent_selection` and `verdict.worker_runtime` provide the
lower-level Prime/RLM dispatch boundary: they intersect OmniRoute inventory
with Prime-visible selectors, use cached bounded health probes, launch an
exact selector, validate the terminal result, and try another eligible
candidate within the runtime budget after a failure.

Use the live commands documented by their help text:

```bash
verdict eligibility --probe --frontier
verdict orchestrate "<goal>" --repo /path/to/repository --max-parallel 3
verdict watch <run-id-or-directory> --once
verdict run-receipt <run-id-or-directory>
```

Use `verdict supervise` when the controller itself needs bounded restart and
resume supervision. See [interview golden path](interview-golden-path.md) for
its required arguments and a complete example.

## 4. Treat model inventory as discovery, not proof

OmniRoute is the inventory and execution boundary. A catalog row does not prove
that a route is entitled, healthy, available, or allowed for the task. The
ADR-036 ladder checks those conditions before selecting a route. In its Prime
configuration, a route also has to be visible in Prime's model registry before
it can be launched.

Do not add static provider allowlists, credentials, or private gateway database
access to Verdict. See [OmniRoute workers](omniroute-workers.md) and the
[routing policy](../specs/ROUTING_POLICY.md).

## 5. Verify in layers

For Verdict Core, run the checks appropriate to the changed surface. The
repository baseline includes:

```bash
uv run pytest -q
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
uv run python -m build
uv run python -m twine check dist/*
uv run bandit -q -r verdict
git diff --check
```

Also run any affected API, CLI, integration, security, benchmark, and
clean-install checks. If a check cannot run, record its exact failure rather
than calling the result green. Obtain independent review of the diff, impact,
security implications, and evidence before merging.

## 6. Finish the pull-request lifecycle

1. Commit an atomic, ticket-referenced slice with no secrets or local state.
2. Push the branch and open or update the pull request with acceptance
   criteria, validation commands, and limitations.
3. Watch checks for the exact head SHA. Repair failures and resolve review
   threads.
4. Merge only after required checks and independent review pass.
5. Verify the target branch contains the expected merge SHA. Update the issue
   or task ledger with the evidence and any remaining limitations.

The final handoff says what changed, what passed, what merged, and what
remains. Writing code alone is not completion.
