# MemoryPlane offline verification

Run the smoke report without provider credentials or network access:

```bash
PYTHONPATH="$PWD" python scripts/memory_offline_smoke.py \
  --output /tmp/verdict-memory-offline.json
```

The report proves only local invariants: SQLite persistence, deterministic FTS
search, deterministic export, redaction, and durable gate-event count. It does
not claim semantic retrieval quality or external-adapter availability.

The report also includes `schema_version`, backend identity, a redacted record
schema/provenance shape, and a digest of that shape. Content, metadata values,
temporary database paths, prompts, and credentials are deliberately omitted.
The output is stable across runs, so it can be copied into a release evidence
directory without turning local paths or memory contents into public evidence.

The canonical record contract includes:

| Field group | Required evidence |
| --- | --- |
| identity | record ID, namespace, key, scope, schema version |
| content | redacted content and SHA-256 content hash |
| authority | source, trust, authority ID, verification flag |
| provenance | source metadata, gate/preflight version, observation time |
| lifecycle | created/updated/expiry timestamps and supersession link |
| safety | confidence, sensitivity, bounded metadata |

Gate decisions are stored as redacted `memory_gate_events` records. An active
record with changed content is blocked until the caller supplies an explicit
`supersedes` record ID. Identical content is idempotent. This makes stale,
contradictory, and unavailable inputs observable without making hosted models
or external memory services prerequisites.
