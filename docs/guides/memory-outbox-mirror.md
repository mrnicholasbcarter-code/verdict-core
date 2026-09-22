# Memory outbox mirror

Create a `MemoryOutbox` on the same SQLite path as `MemoryPlane`, then pass it to the plane. Local writes enqueue eligible envelopes in the same transaction. Run `MemoryMirrorWorker.run_once()` outside the write request.

```python
outbox = MemoryOutbox("memory.db", project="my-project")
with MemoryPlane("memory.db", outbox=outbox) as plane:
    plane.put(record)
MemoryMirrorWorker(outbox, provider).run_once()
```

No outbox is the disabled configuration. It preserves the pre-existing local-only behavior. Retryable failures remain pending with exponential backoff. Auth, schema, protocol, and invalid-request failures become dead letters. Inspect only status, error code, attempts, and timestamps; payload/error rendering is redacted.

For recall, add `SharedMemoryCapabilityProvider(provider, project=...)` after the native providers in `NativeCapabilityResolver`. The existing `memory.search` capability fans out. Provider outages produce named omissions while local context still compiles.
