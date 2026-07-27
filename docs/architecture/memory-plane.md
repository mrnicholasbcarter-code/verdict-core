# Memory Plane

Verdict's durable memory boundary is local-first. `verdict.memory_plane.MemoryPlane`
uses SQLite in WAL mode for persistence and SQLite FTS5 for deterministic lexical
recall. A record carries a namespace, scope, source, trust label, expiry, and
optional supersession link. Search is advisory evidence; it never grants route,
tool, budget, or policy authority.

Ruflo, RuVector, OpenViking, and hosted embedding services may be integrated as
adapters above this boundary. They are optional accelerators and must not be the
only write path, the only recall path, or a required network dependency. Adapter
failures therefore degrade retrieval quality without making local memory
unavailable.

## Operational rules

- Use a single explicitly configured database path per installation/scope.
- Keep source/version/trust metadata with every imported document or session.
- Exclude temporary, generated, credential-bearing, and private runtime data at
  ingestion; do not treat copied upstream claims as authoritative.
- Enforce scope and expiry on reads, not only at ingestion.
- Rebuild or replace indexes transactionally; SQLite remains the source of truth.
- Treat external vector or graph results as ranked evidence and verify them
  against local records before acting.

The migration work in issues #127 and #130 will add canonical MasterDocsRAG
ingestion and session/document adapters against this contract.
