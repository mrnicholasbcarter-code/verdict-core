# Design: BOD-306 brief and continuity contract

## Context

Checkout `a12e5dda59c9632fe83c6d726b3c58ce5bac9cf2`. See proposal.md for why this contract exists.

**KNOWN** entrypoints on this checkout:

- Planner brief: `verdict/orchestration/planner.py:hydrate_node_prompt` calls `verdict/orchestration/worker_context.py:build_worker_context`. That pack already recalls gated local MemoryPlane facts and reports a missing, busy, or unreadable plane as a named omission.
- Autodev brief: `verdict/autodev_run.py:compile_unit_context` does not query memory.
- Context conversion: `verdict/context_intelligence.py:_retrieve_memory` searches a local `MemoryPlane`. `compile_pack` and `plan_context_query` live in the same module. `build_native_providers` does not include `SharedMemoryCapabilityProvider`.
- Shared consumer: `verdict/context_sources.py:SharedMemoryCapabilityProvider.provide` searches an injected `SharedMemoryProvider`. It already returns `healthy_zero_hits`, `provider_auth_failed`, `provider_schema_incompatible`, `provider_timeout`, `provider_unreachable`, `shared_memory_disabled`, and `provider_degraded`. Its class docstring still says optional fail-open. `_shared_hit_to_unit` forces `authority` to `shared-memory-advisory` and does not set verified authority.
- Dispatch: `verdict/orchestration/run.py:run_golden_path` is `verdict orchestrate`. `verdict/orchestration/supervisor.py` is `verdict supervise` and restarts via `verdict orchestrate --resume`. `verdict/worker_runtime.py:_worker_admission` and `cli_run` are the direct worker path. `supervisor_admission.py` is the BOD-157 multi-story governor. It is not a memory gate.
- Durable state already written: `events.jsonl`, `graph.json`, `progress.json`. Spec mismatch already fails closed as `spec_changed` in `run.py`. Capture outbox is `verdict/memory_outbox.py:MemoryOutbox` with `idempotency_key`.
- Hermes plugin: `verdict/harness_hermes_memory.py:_runtime_bridge` has no backend. It reports `memory_backend_not_configured` and captures to a bounded outbox. The comment says `AiMemoryClient` is on security hold.
- Doctor discovery in `verdict/shared_memory.py:discover_shared_memory_setup` still looks for `mcp-memory-service`.
- ADR-033 is Accepted and says shared recall is advisory and the local plane is authoritative. ADR-034 is Accepted and says shared recall fails open. Both are indexed `PARTIALLY_TRUE`. BOD-336's 2026-10-13 text says ai-memory is mandatory and new work refuses. Those ADR texts are not reconciled in this checkout.
- Open PR 986 (`ccd892ee`, branch `swarm/bod-306-queen`) is unmerged. It touches `verdict/autodev_run.py`, `verdict/context_intelligence.py`, `tests/test_mandatory_worker_memory.py`, and `scripts/proof_worker_memory.py`. It is candidate input, not merged-main proof. `scripts/proof_worker_memory.py` does not exist on this checkout.

**NOT AVAILABLE**: A production caller that authenticates ai-memory and injects the result into `hydrate_node_prompt` or `compile_unit_context`.

## Goals / Non-Goals

Goals: freeze the brief record, the continuity packet, the dispatch decision, the file partition, the named tests, and the drill matrix so cards `t_9f5201a6`, `t_83449cd6`, `t_8a2d92f7`, and `t_ed3d23d3` do not re-interpret policy.

Non-goals: a second memory store or client; editing BOD-336, BOD-283, or BOD-80 authority modules; reconciling ADR-033 or ADR-034; installing ai-memory; changing doctor discovery; lifting the Hermes client hold; choosing a free-versus-subscription exception; adding a timer, cap, sample size, or restart budget; measuring `context_lift`; seeding the wiki at setup; marking BOD-306 Done.

## Decisions

1. Call `SharedMemoryCapabilityProvider`. Do not construct a new HTTP client and do not register a second provider. Map its existing gap reasons onto the brief status: `healthy_zero_hits` is `empty`; `provider_auth_failed`, `provider_schema_incompatible`, `provider_timeout`, `provider_unreachable`, `shared_memory_disabled`, and a missing provider are `unavailable` with that reason. `provider_degraded` is `unavailable`, not `empty`.
2. Put the brief record in a new module, `verdict/orchestration/memory_brief.py`. The planner and autodev compilers call it. They do not grow a second recall implementation. If PR 986 has merged `recall_memory_for_task` before the brief slice starts, that slice keeps one recall path and does not add another function beside it. It still must not edit `verdict/context_intelligence.py`.
3. Local MemoryPlane facts stay a labeled side source. They do not fill `status`. Do not delete the existing gated-fact path in `worker_context.py`.
4. Put the continuity packet in a new module, `verdict/orchestration/continuity_packet.py`. Do not edit `verdict/execution_packet.py`. A packet may store an execution-packet id. It is not a subclass and not a second execution ledger.
5. Fence and replacement live in the continuity module. `replace_writer` rejects a missing fence. The module does not choose a route. Resume records the admitted route id the caller already holds. A governing-task mismatch is rejected.
6. Stale spec rejection stays on the existing `spec_changed` path in `run.py`. Acceptance-criterion and ADR mismatches are additional named rejections in `verdict/orchestration/memory_dispatch_gate.py`, called from the gate-owned entrypoints. Do not add a second spec-digest.
7. New-dispatch refusal and in-flight survival live in the gate module. In-flight means the run directory already has `events.jsonl` for a non-terminal generation, or the worker was already admitted. Survival uses that run directory and the existing `MemoryOutbox`. Do not add an outbox and do not change `verdict supervise` defaults `--max-restarts` or `--stall-seconds`.
8. File ownership is exclusive between the brief slice and the continuity slice. The gate slice owns the call sites. See the partition below. A slice that needs a behavior outside its files calls the other module. It does not edit it.

### File partition

Brief slice, card `t_9f5201a6`, may edit only:

- `verdict/orchestration/memory_brief.py` (new)
- `verdict/orchestration/worker_context.py`
- `verdict/autodev_run.py`
- `tests/test_memory_fed_brief.py` (new)

Continuity slice, card `t_83449cd6`, may edit only:

- `verdict/orchestration/continuity_packet.py` (new)
- `tests/test_continuity_packet.py` (new)

Gate slice, card `t_8a2d92f7`, may edit only:

- `verdict/orchestration/memory_dispatch_gate.py` (new)
- `verdict/orchestration/run.py`
- `verdict/worker_runtime.py`
- `verdict/orchestration/supervisor.py`
- `tests/test_memory_dispatch_gate.py` (new)

Drill card `t_ed3d23d3` may add only:

- `tests/test_bod306_entrypoint_drills.py`
- `scripts/proof_bod306_recall_drills.py`

No slice may edit `verdict/context_intelligence.py`, `verdict/context_sources.py`, `verdict/memory_plane.py`, `verdict/memory_outbox.py`, `verdict/shared_memory.py`, `verdict/harness_hermes_memory.py`, `verdict/execution_packet.py`, `verdict/routing_receipt.py`, or `openspec/changes/bod-306-worker-memory-recall/`. The gate slice may call `continuity_packet.fence_writer` and `memory_brief.compile_memory_brief`. It may not change their fields.

### Frozen records

`MemoryBrief.status` is `cited`, `empty`, or `unavailable`. Fields: `project`, `task_id`, `session_id` (`unknown` if absent), `query_scope`, `retrieved_at`, `decisions`, `lessons`, `source_revisions`, `omissions`, `unavailable_reason`, `consumer` (`adapter.shared_memory`), `local_plane_status` (`cited`, `omitted`, or `not_queried`). A citation has `source_id`, `revision`, `content_hash`, `project`, `observed_at`, `kind` (`decision` or `lesson`), `derived`, and redacted text. A lesson names its source outcome id. `authority_verified` stays false.

`ContinuityPacket` fields: `governing_task_id`, `packet_revision`, `research` (finding id, source refs, text, revision), `changed_files`, `last_proof` or explicit none, `exact_next_action`, `governing_revisions` (acceptance-criterion ids, spec revision digest, ADR id and status), `writer_fence`, `replacement_writer_id`, `admitted_route_id`, `authorization_ref`, `refreshed_source_ids`, `retained_source_ids`, `event_ids_applied`, `capture_refs`, `attempt_lineage_id`.

### Named tests

Brief: `tests/test_memory_fed_brief.py::test_cited_decisions_and_lessons_carry_source_revisions`, `test_healthy_zero_hits_is_empty_not_unavailable`, `test_auth_or_infra_failure_is_unavailable_not_empty`, `test_planner_and_autodev_briefs_contain_the_status_field`, `test_local_plane_facts_do_not_satisfy_ai_memory_status`.

Continuity: `tests/test_continuity_packet.py::test_packet_round_trips_after_interruption`, `test_packet_round_trips_after_root_restart`, `test_duplicate_event_does_not_double_apply`, `test_unchanged_research_is_retained`, `test_old_writer_is_fenced_before_replacement`, `test_resume_keeps_governing_task_and_uses_current_authorization`, `test_refresh_skips_unchanged_sources`.

Gate: `tests/test_memory_dispatch_gate.py::test_changed_ac_spec_or_adr_rejects_stale_continuation`, `test_stale_continuation_requires_affected_rehydration`, `test_wrong_project_hit_is_excluded`, `test_secret_canary_is_redacted`, `test_superseded_record_does_not_outrank_verified_decision`, `test_duplicate_replay_is_idempotent`, `test_memory_text_cannot_certify_done_or_weaken_tests`, `test_missing_infra_blocks_new_dispatch_only`, `test_in_flight_work_stays_alive`.

### Drill matrix

Drills run at the real entrypoints. A unit test is not a substitute. Each drill retains raw input, capture id, and attempt lineage. A model paraphrase is not evidence.

| Drill | Entrypoints | Pass condition | Lineage to retain |
| --- | --- | --- | --- |
| Retrieval to accepted result | `verdict orchestrate` (`run_golden_path`) and autodev `compile_unit_context` | Real retrieval appears in the actual worker input. After research is recorded, interrupt. Resume on a different currently admitted route. Requirements and research bytes match. An independent checker can accept or reject the result package. | brief status, source ids, packet revision, `events.jsonl`, attempt id |
| Stale source | `verdict orchestrate` resume path that already emits `spec_changed` | Changed acceptance criterion, spec digest, or ADR id/status rejects continuation and names the required rehydration. Unchanged research remains. | persisted digest, current digest, rejection reason |
| Root restart | `verdict supervise` restart via `orchestrate --resume` | After the controller process is gone, the packet round-trips from the run directory. Governing task is unchanged. Existing restart defaults are not edited. | `progress.json` seq, `events.jsonl`, packet revision |
| Duplicate event | continuity apply path called from the orchestrate resume entry | The same event id does not add a revision or a second finding. | event id, packet revision before and after |
| Quota or stream failure | `verdict supervise` capacity-failure path | The failure does not erase the packet, does not count as success, and does not start a new task. No new stall or restart number is introduced. | failure marker, packet revision, attempt id |
| Wrong project | `compile_unit_context`, `hydrate_node_prompt`, and `cli_run` | A hit for another project is excluded at each entrypoint and counted. | project ids, exclusion count, brief status |

### AC mapping

| Delivery AC | Spec scenario | ADR disposition | Test or drill |
| --- | --- | --- | --- |
| Cited brief, revisions, empty versus unavailable | memory-fed-brief requirements | No ADR edit. Local plane remains labeled side source. ADR-033/034 reconciliation is a blocker, not a silent supersession. | `tests/test_memory_fed_brief.py` and the retrieval drill |
| Continuity, fence, same task, refresh changed or missing | session-continuity-packet requirements | No new restart authority. | `tests/test_continuity_packet.py`, root-restart and duplicate-event drills |
| Stale rejection, isolation, secrets, supersession, outbox, replay, memory cannot certify Done | memory-dispatch-guard first three requirements | Reuse `spec_changed`. Do not add an outbox. | `tests/test_memory_dispatch_gate.py` and stale-source, wrong-project drills |
| Missing infrastructure blocks new dispatch; in-flight stays alive | memory-dispatch-guard last requirement | ADR-034 fail-open is not silently rewritten. New dispatch still refuses, per BOD-336's later text, only at the gate call sites. | `test_missing_infra_blocks_new_dispatch_only`, `test_in_flight_work_stays_alive` |

## Risks / Trade-offs

- [PR 986 lands in `autodev_run.py` while the brief slice also owns that file] → The brief slice rebases onto it and keeps one recall path. It does not edit `context_intelligence.py` to match the PR.
- [ADR-033 still says the local plane is authoritative] → Citations from the shared consumer stay advisory. Workers do not relabel them as authority and do not delete the local path. Changing the ADR is out of this partition.
- [Shared provider is optional in its docstring] → The gate, not the provider module, refuses new dispatch. Provider behavior stays owned by BOD-336.
- [A worker treats this contract as Done] → Tasks stay open. Linear status is not changed here.

## Security / trust implications

Retrieved text is untrusted evidence. Shared hits keep unverified advisory authority, which the existing hit converter already forces. Secret scanning uses the existing redactors before render or retention. A credential canary must be absent from the brief, the packet, and the drill artifact. Memory text cannot approve policy, weaken a test, waive vgate, or certify Done. Project scope is the governing project only. This contract does not print tokens and does not change auth configuration.

## Routing / context / memory implications

No new router, ranker, cap, or memory store. Route identity on resume is whatever existing eligibility already admitted. Context budget stays the budget the selected compiler already has. An unknown tokenizer stays a named gap. Do not adopt the historical 5-item or 1500-character proposal, and do not adopt an unscored similarity formula. `context_lift` is not a delivery measure for these slices. OmniRoute memory headers and `/api/memory` stay out of this contract; BOD-336 already says they are not a Verdict memory source.

## Migration / rollback

No migration and no install. Rollback of a later implementation is deleting the three new modules and the call sites in the gate-owned files. The existing local recall path and the existing `spec_changed` path remain. Do not restart operator services and do not kill a running run as part of landing or rolling back this contract.

## ADR impact

No new ADR. No edit to ADR-033 or ADR-034. Disposition: both remain Accepted and unreconciled with the 2026-10-13 mandatory-backend text. That reconciliation is an explicit blocker for any slice that would change ADR status, delete the local plane path, or make shared recall the authority. It is not a blocker for adding a labeled advisory citation beside the existing path.

## Open blockers

These are unresolved. A later card must not decide them inside its slice.

- ADR-033 and ADR-034 versus the BOD-336 mandatory-backend text. Do not rewrite the ADRs here.
- Hermes `AiMemoryClient` security hold. Do not wire that client.
- Doctor still discovers `mcp-memory-service`. Doctor and install stay with BOD-336.
- Execution-pack choice between strict free exhaustion and a critical-path subscription exception. No exception is approved. Resume uses the current admitted route only.
- Screening caps, timers, sample sizes, and restart budgets. Do not add or change them. Existing `verdict supervise` defaults stay as they are.
- Historical pack score weights and the 5-item / 1500-character injection proposal. Not approved finish-line numbers.
- `context_lift`, held-out recall accuracy, and savings claims. Not in the four delivery ACs.
- The 2026-11-10 seed-and-index setup note on BOD-306. Retained, not owned by these slices, not dropped.
- Paid or metered routes. Not authorized.
- PR 986 exit 0 is not proof of the four delivery ACs.
- `supervisor_admission.py` is not a memory gate. Do not describe it as one.
