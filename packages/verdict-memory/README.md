# verdict-memory

> **SECURITY HOLD:** `AiMemoryClient` is not approved for live consume until backend-security fix pass. Contracts/FakeProvider/outbox unit surface may be used for adapter scaffolding.


Independently installable lifecycle/memory capability package for Verdict and
other harnesses. Provides:

- versioned, language-neutral lifecycle event / capture decision / recall result JSON
- Python public API + canonical golden vectors (native Rust parity unmeasured)
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

## Experimental transport contract

See [docs/TRANSPORT.md](docs/TRANSPORT.md) and captured read-only ai-memory 2.6.6
schemas. The lifecycle envelope is persisted in the supported Markdown `body`,
then read back and validated before ACK. Returned search snippets are not source
objects: scoped reads reconstruct the original event and verify digest, source
identity, tenant and narrower scope. `ExternalMemoryRef.revision` is the validated
event digest. Same-source changed content returns typed `conflict`, never overwrite.

One cooperating writer namespace per host is enforced by an advisory file lock.
The backend has no CAS; cross-host/uncooperative writers are unsupported. Use the
same lock directory for all clients targeting one namespace. Single writer is
a deployment requirement, not a claim of server-side atomicity. Outbox state is
operational only. Captures remain pending across outage; retry after a lost write
response first reads and validates the committed page, without a second write.

No live backend write was performed by the repair owner. The parent-only proof
script requires `--allow-live-write` after independent review and creates a unique
project and source marker, verifies the exact envelope, then deletes only that
validated page in `finally`. It does not remove project containers.
