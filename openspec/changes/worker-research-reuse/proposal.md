# Change: Worker research reuse

## Why

Workers retry the same node and re-read unchanged evidence.

## What Changes

- Add stable research identity, quoted deltas, reuse and projection APIs.
- Label settled evidence and open questions without changing route authority.

## Capabilities

### Modified Capabilities

- `worker-context`: source-bound research reuse.

## Impact

Planner prompts and worker context serialization gain additive metadata. No runtime wiring, dependency, router or memory store changes.

## Evidence boundary

- KNOWN: current worker context and planner APIs exist.
- INFERRED: retries can reuse unchanged snapshots.
- NOT AVAILABLE: live production reuse proof.

## Acceptance criteria

- Unchanged task/source reuse preserves identity and appends only deltas.
- Changed task, repository or source revisions are refused.
