# Feature Specification: Capability hard gate (BOD-100)

## Issue

Linear BOD-100. Parent BOD-97. Depends on BOD-108 Core metadata store.

## Goal

Admit only models whose **Core metadata store** records satisfy the task's
required tools/vision/structured/context. OmniRoute is inventory/health only.

## Functional requirements

1. Derive requirements from the request envelope (`tools`, `vision`,
   `structured_output`, `min_context`) and planner `required_capabilities`.
2. Match against Core store fields with source+version cited on the receipt.
3. Required + null/unknown/stale = named drop. Known `false` for a required
   boolean cap = named `capability_mismatch` drop.
4. Applies to free and paid candidates alike.

## Proof

- Tools-required drops non-tool models.
- Unknown required cap → named drop.
- Free without tools loses to paid with tools (with BOD-109).

## Non-goals

- OmniRoute as metadata SoT
- Inventing agentic scores
