# Spec Delta

## Purpose

Extend EventLog and ExecutionReceipt to carry optional decision_signals in SHADOW mode. Signals recorded next to actual Verdict decision with ZERO behavioral change. Feature flag enables/disables.

## ADDED Requirements

### Requirement: SHADOW mode SHALL record signals with ZERO behavioral change

When VERDICT_DECISION_SIGNALS_MODE=SHADOW, Verdict SHALL:
- Call DecisionSignalProvider.signals()
- Emit EventLog event "decision_signals" with mode="SHADOW", signals, actual_decision
- Carry signals into receipt.decision_signals
- Make IDENTICAL decision as with mode=OFF

#### Scenario: SHADOW with frontier_worthy=0.0
- **WHEN** VERDICT_DECISION_SIGNALS_MODE=SHADOW
- **AND** provider returns frontier_worthy=0.0, confidence=0.95
- **THEN** Verdict decision is IDENTICAL to mode=OFF
- **AND** EventLog contains "decision_signals" event
- **AND** receipt.decision_signals populated

#### Scenario: SHADOW with frontier_worthy=1.0
- **WHEN** VERDICT_DECISION_SIGNALS_MODE=SHADOW
- **AND** provider returns frontier_worthy=1.0, confidence=0.95
- **THEN** Verdict decision is IDENTICAL to mode=OFF
- **AND** EventLog contains "decision_signals" event

#### Scenario: SHADOW with provider failure
- **WHEN** VERDICT_DECISION_SIGNALS_MODE=SHADOW
- **AND** provider fails with failure_class=TIMEOUT
- **THEN** Verdict decision is IDENTICAL to mode=OFF
- **AND** EventLog event contains failure_class=TIMEOUT

### Requirement: mode=OFF SHALL skip provider call

When VERDICT_DECISION_SIGNALS_MODE=OFF (default), Verdict SHALL NOT call DecisionSignalProvider, SHALL NOT emit "decision_signals" event.

#### Scenario: mode=OFF
- **WHEN** VERDICT_DECISION_SIGNALS_MODE=OFF
- **THEN** no provider call
- **AND** no "decision_signals" event in EventLog
- **AND** receipt.decision_signals is None

### Requirement: Invalid mode SHALL treat as OFF with warning

When VERDICT_DECISION_SIGNALS_MODE is invalid (not "OFF" or "SHADOW"), Verdict SHALL treat as OFF and emit warning.

#### Scenario: Invalid mode value
- **WHEN** VERDICT_DECISION_SIGNALS_MODE=INVALID
- **THEN** treated as OFF (no provider call)
- **AND** warning logged

### Requirement: Structured evidence SHALL outrank OpenJev signals

When HTTP status/runtime evidence provides failure_class, that classification SHALL NOT be overridden by OpenJev signals.

#### Scenario: HTTP 429 quota vs OpenJev signal
- **WHEN** HTTP 429 with quota-exhausted code
- **AND** OpenJev signal says "not quota"
- **THEN** failure_class=QUOTA (HTTP wins)

#### Scenario: HTTP 529 vs OpenJev signal
- **WHEN** HTTP 529
- **AND** OpenJev signal says "not overloaded"
- **THEN** failure_class=OVERLOADED (HTTP wins)

## MODIFIED Requirements

<!-- No existing requirements modified -->
