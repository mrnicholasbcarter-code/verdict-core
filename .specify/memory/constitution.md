<!--
Sync Impact Report
- Version change: 1.0.0 → 1.1.0 (MINOR: materially expanded delivery and quality
  governance without removing or redefining an existing principle).
- Modified principles:
  - I. Coordination Is Governance, Execution Is Delivery (added authoritative
    source-of-truth ordering and stale-state verification).
  - III. Repository Boundaries Are Non-Negotiable (added cross-repository contract
    ownership and rollout requirements).
  - IV. Verification Is Part of the Change (defined repository-native quality gates
    and evidence requirements).
- Added sections: Quality Gates.
- Modified sections: Delivery and Review Workflow; Governance.
- Removed sections: none.
- Follow-up TODOs: original ratification date is not recorded and must be recovered if
  historical governance provenance is required.
-->
# Ruflo Portfolio Constitution

## Core Principles

### I. Coordination Is Governance, Execution Is Delivery
Ruflo and related coordination systems MAY recommend routes, record decisions, and
maintain shared state, but they MUST NOT be treated as implementation. Coding agents
remain responsible for inspecting sources, changing files, and running validation.
A coordination record is not evidence that work was executed or completed. When
records disagree, current repository source and Git state, authoritative remote
state, and build or runtime evidence take precedence over transcripts, plans,
handoffs, memory, and task queues, in that order. Stale coordination state MUST be
reconciled or cancelled before it can initiate new work.

### II. Documentation Before Dependencies
Before editing, configuring, diagnosing, or theorizing about a third-party tool,
contributors MUST read authoritative documentation, installed package material,
source, schemas, or native diagnostics. Evidence MUST be recorded before a change
is applied. A plausible assumption is not a substitute for a verified tool contract.

### III. Repository Boundaries Are Non-Negotiable
The workspace root is not a repository. Contributors MUST identify the child
repository that owns a target before performing Git operations, and MUST keep each
repository's changes, commits, branches, and release decisions separate. Shared
manifests and lockfiles have one explicitly designated integration owner; concurrent
writers MUST use isolated worktrees and non-overlapping ownership. Cross-repository
work MUST identify affected interfaces, contract owners, compatibility requirements,
validation in each repository, and an ordered rollout and rollback path before the
first dependent change is merged.

### IV. Verification Is Part of the Change
A change is incomplete until its owning repository's required lint, format, build,
type, test, packaging, and other declared checks pass, together with focused tests,
regression tests, and applicable compatibility, security, performance,
accessibility, and failure-path checks. Requirements that do not apply MUST NOT be
invented as universal numeric thresholds; repositories MUST define measurable
budgets where their risks require them. Claims of completion MUST identify the exact
source state, commands or check runs, and outcomes, and MUST report failures,
unavailable evidence, or skipped checks without substituting inference for evidence.

### V. Safety, Reversibility, and Least Authority
Contributors MUST validate inputs at system boundaries, protect credentials and
private data, and avoid committing secrets or environment files. Destructive,
production, spend, publication, merge, and other hard-to-reverse actions require
explicit authorization or a durable policy gate. A child process or agent MAY drop
capabilities but MUST NOT expand its tools, access, scope, or authority.

## Safety and Repository Constraints

- Changes MUST remain within the requested repository and file scope; documentation
  files are not created unless explicitly requested.
- Temporary work MUST live in designated source, test, documentation, configuration,
  or script directories rather than repository roots.
- Memory, context-hydration, and private configuration data MUST be treated as
  separate ownership domains; unrelated work MUST NOT modify them.
- Public package, deployment, and release claims MUST be checked against the actual
  registry, immutable commit or worktree state, and authoritative CI or service
  receipts.
- When authoritative logs or source evidence are unavailable, the result MUST be
  reported as unknown or blocked rather than repaired by speculation.

## Delivery and Review Workflow

Every substantive change MUST follow this sequence: recall relevant prior decisions;
inspect current source, remote state, dependencies, policy, and health; define scope,
acceptance criteria, ownership, safety limits, and failure cases; implement in an
isolated scope; run focused and regression validation; review affected interfaces,
compatibility, and security; and bind claims to exact source and build evidence.
Implementation MUST NOT begin while essential scope, ownership, dependencies, or
acceptance criteria remain unknown. Work discovered as already complete MUST be
verified and reconciled instead of reimplemented.

Each work unit MUST have one accountable owner through implementation, validation,
and handoff. Peer or independent review MUST examine both the changed path and its
affected interfaces. For portfolio execution, a cross-repository coherence audit
MUST run after every five completed work units, or before release when fewer than
five are in scope, to reconcile contracts, documentation, issue state, and rollout
order.

## Quality Gates

Before a change is merged or represented as complete:

1. **Repository-native checks**: Every check required by the owning repository MUST
   pass with no unreviewed generated diff. The repository's documented commands and
   CI configuration define the applicable lint, format, type, build, test, coverage,
   packaging, and policy gates.
2. **Acceptance and failure paths**: Evidence MUST map the requested acceptance
   criteria to focused validation and include relevant boundary and failure cases.
3. **Regression and contracts**: Affected tests MUST pass, and changed public or
   cross-repository interfaces MUST receive compatibility or contract validation.
4. **Risk-based checks**: Security, performance, accessibility, migration, and
   resource-use checks MUST run when the change affects those risks. Applicable
   repository-defined budgets MUST not regress without a documented exception.
5. **Review**: All blocking review findings MUST be resolved. Autonomous merge is
   permitted only when durable workspace policy authorizes it, the change is
   non-destructive, the remote reports a clean mergeable state, and every required
   check on the exact head revision has concluded successfully.
6. **Evidence disclosure**: Neutral, skipped, unavailable, or non-applicable signals
   MUST be named explicitly. An unknown result MUST NOT be reported as passing.

## Governance

This constitution governs the workspace's development and release practices and
takes precedence over informal coordination records. Child-repository instructions
MAY impose stricter or domain-specific obligations but MUST NOT weaken these rules.
A conflict MUST be resolved in favor of the stricter safe requirement and recorded
when it affects delivery.

A constitution amendment MUST update the version and last-amended date, include a
Sync Impact Report, explain the semantic version bump, and preserve or explicitly
replace still-applicable rules. MAJOR changes remove or incompatibly redefine
obligations and require affected repository-owner review plus a migration plan;
MINOR changes add principles, sections, or materially expanded obligations; PATCH
changes clarify wording without changing obligations. Compliance MUST be checked
during planning, implementation, review, and release. Any exception MUST record its
scope, rationale, approver, evidence, expiry or follow-up, and affected repositories.

**Version**: 1.1.0 | **Ratified**: TODO(RATIFICATION_DATE): historical adoption date not recorded | **Last Amended**: 2026-08-19
