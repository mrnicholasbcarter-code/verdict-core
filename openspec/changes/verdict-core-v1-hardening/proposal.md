# Proposal

## Why

The current V1 plan closes connected gaps between trusted inventory, model qualification, authorized dispatch, checked outcomes, and task-specific feedback. Existing component changes cover parts of this path, but no single contract captures the required end-to-end behavior and acceptance matrix.

## What Changes

- Define the V1 behavior across inventory, admission, selection, dispatch, streaming, outcome evidence, and feedback.
- Coordinate the existing 13-story delivery plan without duplicating component specifications or changing unrelated architecture.
- Preserve performance and task-fit selection without price preference, and consume only checked task outcomes using the recorded smoothed history policy.

## Capabilities

### New Capabilities
- `v1-model-control`: End-to-end V1 requirements for verified model control and feedback.

### Modified Capabilities

## Impact

Coordinates existing model inventory, admission, relay, orchestration, metadata, receipt, and outcome components. No dependency or public API is introduced by this specification alone. Story ownership, dependencies, and acceptance IDs are tracked in `tasks.md`.
