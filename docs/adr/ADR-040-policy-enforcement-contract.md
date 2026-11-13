# ADR-040: Enforced policy contract at native dispatch, review and Done boundaries

- **Status:** Proposed
- **Date:** 2026-11-13
- **Deciders:** Nick (repo owner) — not yet approved; status stays Proposed until Nick accepts
- **Supersedes in part:** [ADR-016](ADR-016-deterministic-policy-and-transition-graphs.md) (hard policy before ranking; this ADR extends the same fail-closed authority to native lifecycle boundaries) and [ADR-036](ADR-036-goal-to-receipt-orchestration.md) (the orchestration golden path; this ADR adds the missing policy gates at dispatch, resume, review and Done). ADR-016 and ADR-036 stay in force; their history is preserved and neither is deleted.
- **Related:** [ADR-0001](0001-verdict-control-plane-invariants.md), [ADR-015](ADR-015-evidence-authority-and-portable-receipts.md), [ADR-030](ADR-030-proof-carrying-decision-plane.md), [ADR-031](ADR-031-prime-workflow-skills.md), [ADR-035](ADR-035-authorized-selected-route-dispatch.md), [ADR-037](ADR-037-supervisor-concurrency-governor.md), [ADR-038](ADR-038-improvement-cycle.md), [boundary map](../policy/bod-340-boundary-map.md), [OpenSpec change bod-340-policy-enforcement-contract](../../openspec/changes/bod-340-policy-enforcement-contract/proposal.md)
- **Story:** BOD-340

## Context

The boundary map (`docs/policy/bod-340-boundary-map.md`) traced every native
entrypoint that can start or advance work on the installed runtime
(`50d10118`): 22 entrypoints, the controls that actually run, 23 bypasses and
29 gaps. The shipped `verdict.operator_policy` authority (`admit_action`,
PR #1000) is wired at only four call sites, and its lifecycle rule allows
`orchestrate.run`, `worker.admit`, `supervise.admit` and `story.ready`
unconditionally. As a result: a goal-mode run can dispatch with no issue, no
acceptance criteria, no packet, no lease and no spec; `ready_decision` skips
the gate entirely when a story has no dependencies; a denied run leaves no
receipt, so `verdict supervise` treats it as a crash and restarts it; and no
code reads ADR status, OpenSpec conformance or the tested source head before
review, PR or Done.

ADR-016 established deterministic hard policy before ranking. ADR-036
established the orchestration golden path and the review-before-COMPLETE
rule. ADR-031 established leases, packets and proof. The pieces exist; the
enforcement contract that binds them at the native boundaries does not. This
ADR records that contract as one decision so the enforcement slices
(dispatch/resume gate, review/integration gate, Done/learning gate, rejection
rules) implement a single governing artifact instead of inventing rules per
slice.

This ADR does not change selection policy. The BOD-342 operator decisions
that govern routing (D6/D7 capable-cheapest selection, the 2026-10-19 consent
scope) apply to the routes these boundaries admit; they are cited, not
re-decided here.

## Decision

One enforcement contract governs the native lifecycle. It reuses the
existing authorities (`operator_policy.admit_action`, `prime_workflow`
`validate_packet`/`validate_dispatch`/`check_bundle`, `prime_state` leases,
`openspec_lifecycle`, `review.OpenCodeReviewer`, `receipt.write_run_receipt`,
`check_dod.py`, vgate/CI). It does not create a second policy service, a
second packet validator, a second reachability or DoD checker, or a blanket
bypass.

### 1. Dispatch/resume gate

Every dispatch or resume through a boundary-mapped entrypoint (E1/E2
`orchestrate`/`supervise`/`improve`, E8 direct worker, E9 Prime bridge, E11
`prime_supervisor`, E12 `ready_decision`, E15 `resume --create`) validates
one packet before any side effect (event, receipt, run dir, worktree,
process, model call):

- **Revisions.** The packet names a material OpenSpec change (or a reviewed
  no-change disposition), the Linear issue and acceptance-criteria revision,
  and the governing ADR id, status and digest. Missing, superseded or
  Proposed-as-Accepted artifacts deny (see §4).
- **Semantic disposition.** The packet declares a disposition: `change` or
  `maintenance-no-change`. Absence denies.
- **Mandatory retrieval.** The BOD-306 memory gate (`memory_dispatch_gate`,
  consuming `memory_brief.compile_memory_brief`) has run and returned a
  non-`unavailable` status for new work, per BOD-336. Running work survives
  retrieval loss.
- **Owned scope and lease.** The story lease (`prime_state`) is held,
  active, unstale, and matches the packet attempt, generation, worktree and
  branch. A resumed run re-checks the same packet; a second dispatch on the
  same story is denied.
- **Live capable route.** The existing permit path (`HealthCache.commit_dispatch`,
  `revocation_epoch`, passport confirmation) stays authoritative; the gate
  records the permit id in the audit record.
- **Bound authorization.** `admit_action` runs with the packet's issue, AC,
  brief, ownership, safety and proof references populated, so the lifecycle
  rules can bind them. The policy source is the operator-approved source
  (§6).

The gate runs before side effects. A denial emits a terminal event and a
receipt (or a durable refusal outcome) so `supervise` and `improve` do not
restart or learn from a policy denial.

### 2. Review/integration gate

Review and integration (E6 `DagRuntime._conclude`/`_review_with_bound`, E7
`completion_verdict`, E17 `check-proof`/`open-pr`/`merge-guard`) verify the
same packet identity recorded at dispatch and the exact tested source head
SHA. A resumed run that reuses `prior_validated` nodes re-checks packet,
spec and ADR revisions before reuse; a mismatch invalidates only the
affected nodes and revalidates them. A proof or review bound to a different
head than the packet is BLOCKED.

### 3. Done/learning gate

`DONE` (E18: finish/closeout, `check_dod.py`, `prime_workflow.transition`)
requires both an independent whole-task acceptance (a checker other than the
author re-ran an acceptance command and recorded the raw output) and
installed-main acceptance (the same check green on the installed runtime,
per the DoD definition in AGENTS.md). The `learned` lesson stage of
`verdict improve` skips runs that ended in a policy denial or policy BLOCKED,
so policy denials are never turned into planner lessons.

### 4. Rejection rules

The validator rejects, with a named reason, before any side effect:

- missing material OpenSpec change or ADR for code-changing work without a
  reviewed no-change disposition;
- a superseded ADR cited as governing;
- a conflict between code, spec and ADR at the bound revisions;
- a Proposed ADR represented as Accepted (or any status mismatch with the
  ADR file's `Status:` line);
- an unauthorized weakened scenario or assertion: a diff that removes or
  loosens `#### Scenario` blocks or `assert` lines without a linked,
  reviewed disposition;
- an incompatible shared-interface change under `contracts/`, `schemas/` or
  `verdict/schemas/` that fails `verdict compat` (ADR-024).

### 5. Maintenance path

Legitimate no-change maintenance (docs, comments, refactors with no spec
impact) links the current governing artifacts with a specific disposition of
`maintenance-no-change` and a reviewer id. It is a packet field validated by
the same validator; it is not an override token and it cannot authorize a
behavior change.

### 6. Invariants preserved

This contract preserves every existing control: single-use dispatch permits,
revocation epochs, SSE stream terminal and close-once, idempotency keys,
secret redaction, all vgate and CI protections, running-work safety (running
work survives retrieval or policy-source loss; only new dispatch is refused),
and the DoD. A change to a governing source (spec, ADR, packet) invalidates
only the evidence bound to it, never the whole run. There is no competing
policy service and no blanket bypass: every refusal is a named deny from the
one authority, and every override is a single-use, operator-bound token
against the approved source.

## Open questions (operator policy choices not decided here)

These are recorded, not decided. Each needs an operator decision before the
enforcement slices that depend on it can be called complete:

1. **Approve this contract?** Status is Proposed. Nick's acceptance (or
   amendment) flips it to Accepted; until then the enforcement slices
   implement against a Proposed ADR and must not claim it as governing.
2. **Approve the policy digest.** The lifecycle rules that bind issue, AC,
   brief and ADR references change the approved policy digest; per BOD-342
   the operator approves digest changes. The rules must not be
   self-approved (G15).
3. **Override token signing.** Who holds the operator signing key, and is a
   durable nonce store (G16) required before any override path is enabled?
4. **Policy source authority.** Drop `VERDICT_OPERATOR_POLICY_DIR` outside
   tests, or pin the approved digest in code/doctor (G15)?
5. **`source.edit`/`subagent.dispatch` precedence.** The current deny-first
   rules refuse compliant requests (G14/B5). The allow path and its
   precedence are an operator decision on the reviewed digest.
6. **Prime native boundary (G13).** Prime 0.10.2 has no pre-tool hook.
   Deliver a pinned adapter/patch, or accept "doctor red / unadmitted" until
   one exists. Either is an operator choice; neither is claimed here.
7. **Multi-story supervisor flag (B20).** `VERDICT_MULTI_STORY` stays off
   by default (ADR-037); whether the BOD-340 gates under it are required
   before any enable is an operator sequencing decision.

## Consequences

- The enforcement slices have one contract to implement and one validator to
  extend; the boundary-map gaps G1–G29 each map to a requirement and
  scenario below.
- No entrypoint is claimed enforced until its gate runs on the installed
  runtime and the sentinel that must flip from allow to deny actually flips.
- The ADR stays Proposed; the enforcement code and DoD evidence follow the
  operator decisions above, not precede them.