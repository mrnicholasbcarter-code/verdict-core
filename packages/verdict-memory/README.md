# verdict-memory

> **SECURITY HOLD:** `AiMemoryClient` is not approved for live consume until backend-security fix pass. Contracts/FakeProvider/outbox unit surface may be used for adapter scaffolding.


Independently installable lifecycle/memory capability package for Verdict and
other harnesses. Provides:

- versioned, language-neutral lifecycle event / capture decision / recall result JSON
- Python public API + Rust-compatible canonical golden vectors
- bounded durable SQLite outbox (operational, not a retrieval store)
- authenticated client for existing ai-memory (2.6.x)

**API status:** experimental (`0.1.0a1`). Not a frozen 1.0. Compatible evolution
within the `lifecycle-event/v1` schema family until a validated vertical freezes.

## Install (isolated)

```bash
uv pip install ./packages/verdict-memory
# or: python -m pip install ./packages/verdict-memory
```

No `verdict` package, source tree, or `PYTHONPATH` required.

## Ownership

Package path: `packages/verdict-memory/**` (BOD-80 P owner).
Verdict consumer wiring is owned by E; do not edit root `pyproject` or `verdict/*` from this package lane.
