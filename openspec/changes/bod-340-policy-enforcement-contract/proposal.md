# BOD-340: Enforced policy contract at native dispatch, review and Done boundaries

## Why

The boundary map (`docs/policy/bod-340-boundary-map.md`) shows that the
shipped operator-policy authority is wired at only four call sites and that
its lifecycle rule allows `orchestrate.run`, `worker.admit`,
`supervise.admit` and `story.ready` unconditionally. Goal-mode runs can
dispatch with no issue, acceptance criteria, packet, lease or spec; a denied
run is restarted by `supervise` and turned into a lesson by `improve`; and
nothing validates story/spec/ADR revisions, semantic disposition, mandatory
retrieval, owned scope, bound authorization, the exact tested head, or the
Done acceptance path at the native boundaries. ADR-040 records the contract;
this change is the spec the enforcement slices implement.

## What Changes

- Add one capability, `policy-enforcement-contract`, whose requirements
  below are the enforcement contract every boundary-mapped entrypoint
  (boundary-map E1–E22) must satisfy before it can be called enforced.
- The contract names the existing authorities it reuses
  (`admit_action`, `validate_packet`/`validate_dispatch`/`check_bundle`,
  `prime_state` leases, `openspec_lifecycle`, `OpenCodeReviewer`,
  `write_run_receipt`, `check_dod.py`, vgate/CI) and forbids a second policy
  service, a second validator, or a blanket bypass.
- **BREAKING (for the enforcement slices, not for callers):** entrypoints
  that today allow without a packet (E1/E2 orchestrate, E8 direct worker,
  E11 supervisor, E12 ready_decision, E15 `resume --create`) will deny until
  they supply a valid packet. This is the intended fail-closed change; the
  sentinel evidence in the boundary map is the flip list.

## Capabilities

### New Capabilities

- `policy-enforcement-contract`: the dispatch/resume, review/integration and
  Done/learning gates plus the rejection and maintenance rules, applied to
  the native execution boundaries named in the boundary map.

### Modified Capabilities

- `prime-dispatch-packet-gate`: the new contract extends the same packet/lease discipline to every boundary-mapped entrypoint, not only the Prime bridge. This capability is defined in the sibling change `bod-340-prime-dispatch-packet-gate` and is not yet archived into `openspec/specs/`; no existing requirement is weakened, and the bridge gate remains the E9 implementation of the dispatch gate.

## Impact

- Code: `verdict/operator_policy` rules, `verdict/orchestration/{run,
  runtime,supervisor,ready_gate,receipt,cli}.py`, `verdict/worker_runtime.py`,
  `scripts/prime_workflow.py`, `scripts/prime_supervisor.py`,
  `verdict/actions/extra._action_resume`, `verdict/cli.py` autodev paths —
  each only as needed by its gate. Hotspot: `run.py`, `worker_runtime.py`,
  `supervisor.py` are owned by the unmerged BOD-306 gate slice
  (`wt/t_8a2d92f7`); coordinate before editing.
- Policy: rule changes alter the approved digest; the new digest needs
  operator approval (ADR-040 open questions; BOD-342 decision log).
- ADR: [ADR-040](../../docs/adr/ADR-040-policy-enforcement-contract.md)
  (Proposed) supersedes ADR-016 and ADR-036 in part for the lifecycle
  boundaries; both stay in force.
- Evidence: `docs/policy/bod-340-boundary-map.md` is the entrypoint and
  bypass authority; every scenario below names the entrypoint(s) it applies
  to.
- Not claimed: live acceptance on the operator host, and no Prime native
  interception (G13) until a pinned adapter exists or doctor reports
  unsupported.

## ADR disposition

ADR-040 is the governing ADR for this contract and is **Proposed**. Its open
questions (digest approval, override signing, policy-source authority,
rule precedence, Prime native boundary, multi-story sequencing) are operator
decisions recorded, not settled. This change implements the contract; it
does not mark the ADR Accepted.
