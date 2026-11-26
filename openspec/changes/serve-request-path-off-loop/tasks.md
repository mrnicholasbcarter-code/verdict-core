# Tasks

## 1. Verify the chain once per evidence read

- [x] 1.1 One walk per `_current` call, two per `append_event`: `tests/test_durable_receipts.py::test_current_makes_exactly_one_chain_walk`, `::test_append_event_walks_once_per_current_call`, `::test_find_by_selector_and_missing_key_walk_once`
- [x] 1.2 Tampering anywhere in the scope still fails every read, and nothing is cached across calls: `tests/test_durable_receipts.py::test_tamper_anywhere_in_scope_still_fails_every_read`, `::test_verified_reads_refuse_a_tampered_scope_before_any_read`, `::test_a_later_tamper_is_caught_by_the_next_call`
- [x] 1.3 The public readers still verify every call and take no skip argument; verified reads walk once, are bound to their scope and cannot be kept: `tests/test_durable_receipts.py::test_the_public_readers_still_verify_every_call`, `::test_public_readers_take_no_skip_argument_and_always_walk`, `::test_verified_reads_walk_once_and_are_bound_to_their_scope`, `::test_verified_reads_cannot_be_kept_and_used_after_their_call`

## 2. Run the request path's state work on one serial lane

- [x] 2.1 `/health` and the cheap 401 answer in under 0.2 s while a route decision is held: `tests/test_serve_event_loop_responsive.py::test_health_and_cheap_refusals_answer_while_a_route_decision_is_held`
- [x] 2.2 The decision, the evidence start and finish, the permit commit and the observation run on the lane thread, for the relay and for `/v1/route` and `/route`: `tests/test_serve_event_loop_responsive.py::test_the_request_path_state_work_runs_on_the_one_lane_thread`, `::test_the_route_endpoint_state_work_also_runs_on_the_lane`
- [x] 2.3 One serial order for two concurrent requests, and the lane's own order: `tests/test_serve_event_loop_responsive.py::test_evidence_writes_keep_one_serial_order_for_two_concurrent_requests`, `::test_lane_runs_jobs_one_at_a_time_in_submission_order_with_the_callers_context`
- [x] 2.4 The request instant and the verified assignment are visible inside the lane: `tests/test_serve_event_loop_responsive.py::test_request_time_and_assignment_are_visible_inside_the_lane`
- [x] 2.5 A denial makes no upstream call and mints no proof; an error is the same HTTP response: `tests/test_serve_event_loop_responsive.py::test_a_denial_runs_on_the_lane_and_makes_no_upstream_call_and_no_proof`, `::test_an_error_inside_the_lane_surfaces_as_the_same_http_response`; `tests/test_admission.py` and the assigned-lane and relay tests are unchanged and pass
- [x] 2.6 A cancelled waiter does not drop its queued job, a stream terminal is awaited, the buffered and stream bookkeeping keep their order in one job: `tests/test_serve_event_loop_responsive.py::test_a_cancelled_waiter_does_not_drop_the_job_it_queued`, `::test_a_stream_terminal_is_awaited_on_the_lane_even_when_the_closer_is_cancelled`, `::test_buffered_bookkeeping_keeps_its_order_in_one_lane_job`, `::test_stream_bookkeeping_keeps_its_order_in_one_lane_job`
- [x] 2.7 A service with no lane decides inline: `tests/test_serve_event_loop_responsive.py::test_a_service_without_a_lane_decides_inline_as_before`
- [x] 2.8 A request task cancelled while its row is being started or its permit committed ends the row: `tests/test_serve_event_loop_responsive.py::test_a_task_cancelled_while_its_row_is_being_started_or_its_permit_committed_ends_the_row`
- [x] 2.9 The lane is made and released with the app, however it ends, and closing it drops no queued job: `tests/test_serve_event_loop_responsive.py::test_the_lane_belongs_to_the_app_and_is_shut_down_with_it`, `::test_the_lifespan_releases_the_lane_when_the_app_exits_with_an_error`, `::test_closing_the_lane_drains_queued_jobs_within_a_bound_and_then_refuses_new_ones`, `::test_closing_a_lane_with_a_stuck_job_gives_up_waiting_but_drops_nothing`, `::test_a_cancelled_close_still_stops_the_worker`

## 3. Measure on a spare serve

Verify against the real-size receipt store, never the installed serve.

- [x] 3.1 Before and after on a spare serve with a copy of the real receipt store and a fake gateway: per-request wall time and `/health` latency with one and four clients, recorded in the pull request

## 4. Spec

Test and verify: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/serve-request-path-off-loop` and `npx -y @fission-ai/openspec@1.13.2 validate serve-request-path-off-loop --strict`.

- [x] 4.1 `openspec validate serve-request-path-off-loop --strict`
- [ ] (post-merge) 4.2 On installed main, `/health` stays under the acceptance probe's 2 s while one OpenCode worker runs: poll `curl -s -m 2 -o /dev/null -w '%{time_total}\n' http://127.0.0.1:8000/health` every second through one `verdict orchestrate` OpenCode run and record the slowest answer
