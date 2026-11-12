# Spec Delta

## Purpose

Defines checked terminal outcomes, bounds and close-once for relayed SSE streams.

## ADDED Requirements

### Requirement: Stream outcome follows the terminal event, not byte framing

`SSETerminalParser` SHALL give the same outcome and byte-identical pass-through for any chunking. A chat stream succeeds only on `finish_reason`, an optional usage event, then `[DONE]`. `[DONE]` without a finish reason, EOF without `[DONE]`, and data after `[DONE]` SHALL fail closed. A Responses stream maps `response.completed` to success, `response.failed` to failed, `response.incomplete` to incomplete, and EOF without a terminal to an abrupt-EOF error. The `data:` field SHALL drop exactly one leading space.

#### Scenario: Chat and Responses terminals
- Proof: `tests/test_stream_terminal.py::test_sse_terminal_identity_and_close_once_are_chunking_independent`, `test_chat_finish_usage_done_ordering_is_success`, `test_chat_tool_call_finish_reason_is_checked_success`, `test_chat_done_without_finish_reason_fails_closed`, `test_chat_eof_after_finish_without_done_is_abrupt`, `test_data_after_done_fails`, `test_responses_terminal_states_map_to_outcomes`, `test_responses_abrupt_eof_fails_closed`, `test_sse_data_field_removes_exactly_one_leading_space_per_spec`, `test_sse_extra_spaces_after_colon_are_payload_not_framing`

### Requirement: Stream buffers are bounded

The parser SHALL fail when a pending event exceeds 256 KiB, a partial line exceeds the buffer, the stream exceeds 16 MiB, or it exceeds 100,000 events.

#### Scenario: Overflow
- Proof: `tests/test_stream_terminal.py::test_aggregate_event_overflow_always_fails`, `test_multiline_accumulation_overflow_fails`, `test_undelivered_partial_line_overflow_fails`, `test_total_byte_and_event_limits_fail`

### Requirement: Upstream is closed once with one outcome

`ValidatedSSEStream` SHALL close the upstream exactly once and record exactly one `StreamOutcome` for completion, cancellation, close error and upstream transport error (`transport_error`). `StreamState` SHALL be a closed vocabulary.

#### Scenario: Close paths
- Proof: `tests/test_stream_terminal.py::test_chain_cancellation_closes_upstream_once_with_one_outcome`, `test_cancelled_iteration_aborts_and_closes_once`, `test_upstream_transport_error_closes_once_and_records_one_outcome`, `test_close_once_records_close_error_and_never_recloses`, `test_stream_state_is_a_closed_vocabulary`

### Requirement: Stream identity never mints proof from substitution

The stream identity check SHALL NOT mint proof for a substituted model, SHALL keep opaque aliases unverifiable, and SHALL report missing attestation as unavailable.

#### Scenario: Identity
- **THEN** substitution mints no proof, opaque aliases stay unverifiable, and missing attestation is unavailable
- Proof: `tests/test_stream_terminal.py::test_identity_e1_substitution_never_mints_proof`, `test_identity_e2_opaque_alias_stays_unverifiable`, `test_identity_e3_missing_attestation_is_unavailable`, `test_replay_safety_membership_is_unchanged`

### Requirement: The API relay uses the stream terminal module

The relay SHALL record evidence from the stream outcome: failed and incomplete terminals SHALL NOT be recorded as success, and a mid-stream read timeout SHALL NOT be retried.

#### Scenario: Relay wiring
- Proof: `tests/test_stream_relay_wiring.py::test_standard_chat_stream_is_relayed_and_checked_success`, `test_chat_done_without_finish_reason_fails_closed`, `test_mid_stream_read_timeout_closes_once_is_not_success_and_is_not_retried`, `test_responses_terminal_event_decides_outcome`
