# Autonomous development workflow

This is the operating contract for changes to Verdict and its companion
repositories. It is evidence-driven: an agent may implement a bounded ticket,
but it may not call work complete until the code, documentation, checks,
review, and pull-request state agree.

## 1. Establish context before using tools

Before designing or editing a feature, the lead agent must:

1. Read `AGENTS.md`, contributor guidance, architecture records, relevant
   specifications, and the issue acceptance criteria.
2. Resolve and query current documentation for unfamiliar libraries, APIs,
   CLIs, services, and protocols. Use Context7 or first-party documentation;
   do not rely on remembered API shapes.
3. Ingest relevant documentation into project RAG/memory. Retain only
   source-attributed, sanitized knowledge: never store credentials,
   authorization headers, private URLs, raw prompts, provider responses, or
   sensitive task text. If a RAG connector is unavailable, store a sanitized
   knowledge card with version, date, and limitations.
4. Inventory the source tree, tests, manifests, workflows, installed
   libraries, global tools, MCP servers, and dirty-worktree changes. Preserve
   existing user changes; never silently reset them.

The output is a short context record containing sources, assumptions,
limitations, and the evidence commands to run.

## 2. Use Code Review Graph before implementation and review

Use Code Review Graph before changing non-trivial code and again after the
change:

```text
get_minimal_context(task=..., repo_root=...)
search_graph / semantic_search_nodes for relevant symbols
query_graph(pattern=callers_of|callees_of|imports_of|tests_for, target=...)
get_impact_radius(changed_files=..., repo_root=...)
get_affected_flows(changed_files=..., repo_root=...)
get_review_context(changed_files=..., include_source=true, repo_root=...)
```

Use the repository's graph MCP server when available. Rebuild or incrementally
update a stale graph before relying on its results. Record affected flows,
blast radius, test gaps, and follow-up tickets. Graph output informs review;
it does not replace tests or independent review.

## 3. Make work ticket-backed and bounded

Every implementation slice maps to an issue or a newly created issue with
acceptance criteria. A work package has one owner, a non-overlapping file
boundary, tests and docs, an independent reviewer, evidence commands, and a
disposition for discoveries that are not fixed. Use a hierarchical Ruflo swarm
for multi-file, cross-module, security, performance, or cross-repository work.
Use separate worktrees or disjoint ownership; workers must not revert another
worker's changes.

## 4. Select workers through the documented model plane

OmniRoute is the provider/model transport boundary. The orchestrator reviews
the live model catalog and selects a concrete model or documented virtual route
for each task. Use provider-diverse, non-frontier workers for research,
mechanical edits, and low-risk tests when appropriate; reserve the strongest
available model for architecture, security, integration, and final
verification.

Catalog membership is not availability. A missing, unauthorized, timed-out,
malformed, or unavailable OmniRoute management/MCP response is `unknown`, not
healthy. Protected work fails closed without fresh runtime truth. Do not put
provider allowlists, credentials, or private OmniRoute database access in
Verdict. See [OmniRoute workers](omniroute-workers.md) and the
[routing policy](../specs/ROUTING_POLICY.md).

## 5. Verify in layers

For Verdict Core, the baseline commands are:

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

Also run clean-install wheel/sdist smoke, API/CLI smoke, and repository-specific
integration, security, benchmark, or package checks. If a toolchain limitation
prevents a check, record the exact failure and create or update a ticket; do
not report the gate as green. Update Code Review Graph and obtain independent
review of the diff, impact, security implications, and test evidence before
committing.

## 6. Finish the pull-request lifecycle

1. Commit an atomic, ticket-referenced slice with no secrets or local state.
2. Push a reviewable branch and open/update the PR with acceptance criteria,
   graph findings, commands, and limitations.
3. Watch checks for the exact head SHA. Read failures, repair the branch or
   workflow, rerun checks, and resolve review threads.
4. Rebase or repair conflicts instead of leaving a conflicted PR abandoned.
5. Merge only after required checks and independent review are green.
6. Verify the target `main`/`master` branch contains the expected merge SHA and
   update the issue/task ledger with evidence.
7. Explicitly close obsolete or blocked PRs with the reason and evidence; no
   stale work is silently left open.

## 7. Record learning without leaking data

Use Ruflo pre-task, task, memory, and post-task hooks to retain sanitized
patterns and outcomes. Record verified successes and failures so the learning
loop cannot reward worker mirages. SONA/ReasoningBank may improve orchestrator
selection, but learned results cannot bypass Verdict's deterministic policy or
eligibility gate.

The final handoff states what changed, what passed, what merged, what remains,
and which limitations are open. “Finished” means acceptance criteria and
evidence are satisfied, not merely that code was written.
