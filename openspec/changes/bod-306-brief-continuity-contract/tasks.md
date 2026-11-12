# Tasks

Implementation of this change is later work. Do not mark BOD-306 Done from this file. Do not change Linear status in these tasks. Card `t_f3c37e32` owns Linear sync after the drills.

## 1. Brief slice

Owner: card `t_9f5201a6`. Owned files: `verdict/orchestration/memory_brief.py`, `verdict/orchestration/worker_context.py`, `verdict/autodev_run.py`, `tests/test_memory_fed_brief.py`. Do not edit the continuity module, the dispatch gate, `run.py`, `worker_runtime.py`, `supervisor.py`, or any BOD-336/283/80 authority file.

- [ ] 1.1 Add `compile_memory_brief` with status `cited`, `empty`, or `unavailable`, source revisions, and derived lessons kept distinct from decisions. Verify `tests/test_memory_fed_brief.py::test_cited_decisions_and_lessons_carry_source_revisions` and `test_healthy_zero_hits_is_empty_not_unavailable` and `test_auth_or_infra_failure_is_unavailable_not_empty`.
- [ ] 1.2 Call that function from `build_worker_context` and `compile_unit_context` so the planner brief and the autodev brief both contain the status. Keep one recall path if PR 986 has already added one. Verify `test_planner_and_autodev_briefs_contain_the_status_field`.
- [ ] 1.3 Keep local MemoryPlane facts labeled and unable to satisfy the shared status. Verify `test_local_plane_facts_do_not_satisfy_ai_memory_status`.

## 2. Continuity slice

Owner: card `t_83449cd6`. Owned files: `verdict/orchestration/continuity_packet.py`, `tests/test_continuity_packet.py`. Do not edit brief files or dispatch files.

- [ ] 2.1 Round-trip research, packet revision, changed files, last proof, and exact next action after interruption and after the writer process is gone. Verify `tests/test_continuity_packet.py::test_packet_round_trips_after_interruption` and `test_packet_round_trips_after_root_restart`.
- [ ] 2.2 Reject a duplicate event id and retain unchanged research by finding id. Verify `test_duplicate_event_does_not_double_apply` and `test_unchanged_research_is_retained`.
- [ ] 2.3 Fence the previous writer before `replace_writer` succeeds. Resume only for the same governing task, recording the caller-supplied admitted route. Refresh only changed or missing source ids. Verify `test_old_writer_is_fenced_before_replacement`, `test_resume_keeps_governing_task_and_uses_current_authorization`, and `test_refresh_skips_unchanged_sources`.

## 3. Gate slice

Owner: card `t_8a2d92f7`. Owned files: `verdict/orchestration/memory_dispatch_gate.py`, `verdict/orchestration/run.py`, `verdict/worker_runtime.py`, `verdict/orchestration/supervisor.py`, `tests/test_memory_dispatch_gate.py`. Call the brief and continuity modules. Do not edit them.

- [ ] 3.1 Reject a changed acceptance criterion, spec digest, or ADR id/status, reusing `spec_changed` for the spec digest, and require affected rehydration. Verify `tests/test_memory_dispatch_gate.py::test_changed_ac_spec_or_adr_rejects_stale_continuation` and `test_stale_continuation_requires_affected_rehydration`.
- [ ] 3.2 Enforce wrong-project exclusion, secret redaction, supersession, idempotent replay through the existing outbox key, and the rule that memory text cannot certify Done or weaken a test. Verify `test_wrong_project_hit_is_excluded`, `test_secret_canary_is_redacted`, `test_superseded_record_does_not_outrank_verified_decision`, `test_duplicate_replay_is_idempotent`, and `test_memory_text_cannot_certify_done_or_weaken_tests`.
- [ ] 3.3 Block new orchestrate, supervise, autodev, and direct-worker dispatch when the shared consumer is unavailable, and leave an in-flight run alive on `events.jsonl`, `graph.json`, `progress.json`, and the existing outbox. Verify `test_missing_infra_blocks_new_dispatch_only` and `test_in_flight_work_stays_alive`.

## 4. Entrypoint drills

Owner: card `t_ed3d23d3`. Owned files: `tests/test_bod306_entrypoint_drills.py`, `scripts/proof_bod306_recall_drills.py`. Do not edit production modules owned by sections 1–3. If an entrypoint cannot be reached, record the gap. Do not weaken a test to hide it.

- [ ] 4.1 Run the retrieval, interruption, replacement, stale-source, root-restart, duplicate-event, quota-or-stream, and wrong-project drills at the entrypoints named in design.md. Verify each drill has a command, raw result, lineage id, and pass or fail in the proof script output.
