# Legacy Memory Archival, Ingestion, and Erasure Specification

## Overview

To achieve a 100% local-first, zero-external-dependency memory architecture, Verdict completely eliminates legacy ununified memory systems, specifically **OpenViking**, legacy RAG JSON files, and uncoordinated local databases.

This specification details the transactional pipeline for archiving legacy memory artifacts, ingesting their records into the unified Verdict `MemoryPlane` (`.verdict/memory.db`), and safely erasing legacy files. The MasterDocsRAG SQLite store is migrated through the explicit canonical importer described in the [MasterDocs Canonical Migration](#masterdocs-canonical-migration-explicit-import) section and is **never** read as an authority.

---

## Targeted Legacy Artifacts

The migration module (`verdict/memory_migration.py`) scans for and targets:
1. **OpenViking Stores**: `~/.openviking`, `.openviking/`, OpenViking SQLite DBs, and repair scripts (`repair-openviking-*.sh`).
2. **Legacy RAG Contexts**: `.pi_rag_context.json`, `MasterDocsRAG.db`.
3. **Legacy Code Graph DBs**: `.code-review-graph/`.
4. **Tool-Specific Memory Dumps**: `.codex-memory/`, uncoordinated `.claude-flow/memory/*.json`.

MasterDocsRAG (`.MasterDocsRAG.db` or caller-supplied path) is a **legacy optional source, never a MemoryPlane authority**. It is imported only through the explicit `MasterDocsAdapter` boundary (`verdict/memory_masterdocs_adapter.py`), which emits deterministic canonical chunks and a machine-readable report and never mutates the source database.

---

## 4-Step Migration Pipeline

```
┌─────────────────────────┐     ┌─────────────────────────┐
│ 1. DETECT & AUDIT       │ ──> │ 2. CREATE ARCHIVE       │
│ Locate legacy files     │     │ .tar.gz bundle          │
└─────────────────────────┘     └─────────────────────────┘
                                             │
                                             ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│ 4. SAFE ERASE & PURGE   │ <── │ 3. INGEST TO MEMORYPLANE│
│ Remove legacy files     │     │ Ingest into memory.db   │
└─────────────────────────┘     └─────────────────────────┘
```

### Step 1: Detect & Audit (`detect_legacy_memory_artifacts`)
Scans host directories (`~` and repository root) for known legacy file patterns and outputs a detailed `LegacyArtifactReport`.

### Step 2: Create Compressed Archive (`archive_legacy_memory`)
Bundles all detected legacy memory files into a timestamped, gzip-compressed tarball inside `.verdict/archive/memory_archive_<timestamp>.tar.gz` before any destructive action.

### Step 3: Ingest Records into MemoryPlane (`ingest_legacy_artifacts`)
Reads document entries, context sessions, and code entity nodes from legacy stores and writes canonical `MemoryRecord` entries into the local `MemoryPlane` database under namespaces:
- `legacy_rag`
- `legacy_session`
- `code_graph`
- `documents`

### Step 4: Safe Erasure & Cleanup (`purge_legacy_artifacts`)
Removes the original legacy files and directories after verifying successful archival and ingestion. Ensures zero active code paths reference OpenViking or legacy RAG endpoints. Legacy MasterDocs SQLite files are erased only after their canonical chunks and manifest have been durably imported and verified.

---

## MasterDocs Canonical Migration (Explicit Import)

The `MasterDocsAdapter` is the only sanctioned path from a legacy MasterDocsRAG SQLite file into the `MemoryPlane`. It is a versioned, deterministic, read-only importer. It does **not** auto-discover sources, does **not** modify the source database, and is **not** a MemoryPlane authority.

### Versioned Chunks and Manifests

- **Adapter version**: `MASTERDOCS_ADAPTER_VERSION = "2"`; **schema version**: `MASTERDOCS_SCHEMA_VERSION = "1"`. Both are emitted in every `MasterDocsIngestionReport`.
- Each accepted document is normalized (NFC, BOM-stripped, line-ending normalized) and split into bounded chunks (`DEFAULT_CHUNK_SIZE = 1200` UTF-8 bytes) on `\n\n` boundaries. Each chunk carries byte-span offsets and the parent document hash.
- `canonicalize_db_records(...)` returns a `MasterDocsIngestionResult` containing canonical `MemoryRecord`-shaped dictionaries and a frozen `MasterDocsIngestionReport`. The bundle (records + report) is deterministically hashed (`manifest_hash`, SHA-256 over the sorted-keys JSON) so a migration pass is reproducible and auditable.
- Import into the plane is a separate, explicit step via `canonicalize_db(...)`, which writes the records only after the caller opts in. Adapters never write to the plane automatically.

### Read-Only Source Access

- The source SQLite file is opened with a read-only URI (`file:<path>?mode=ro`) and `PRAGMA query_only=ON`. The importer never writes to the source database and never executes mutating statements.
- A legacy SQLite input must be explicitly enabled by the caller (`allow_legacy_sqlite=True`); the default refuses private database inputs and directs callers to a validated exported manifest.
- The resolved path must exist, be a regular file, be inside an absolute allowlisted root, and not be a symlink. Inputs outside the allowlist are rejected as `rejected` before any table inspection.

### `corpus_fts` Rejection

- If the source database contains a `corpus_fts` table, the adapter returns `status="rejected"` with reason `"corpus_fts requires an explicit versioned migration"` and imports **nothing**. The divergent FTS-backed schema is not silently mapped; it requires a future, separately versioned migration descriptor.
- The adapter accepts only the `documents` or `doc_fts` tables and requires `path` and `content` columns; any other shape is rejected with an explicit reason.

### Path and Quarantine Policy

- Quarantined path components (case-insensitive) include cache, generated, temp, vendor, and related variants (`_QUARANTINED_PARTS`). Paths whose stem matches `(?:[._-](?:generated|gen|tmp|temp))$` are also quarantined.
- `allow_tmp=False` (the default) additionally rejects `tmp`/`/tmp/` locations. Quarantined paths are reported in `quarantined_paths` and counted in `quarantined`, and their content is never emitted as canonical chunks.
- Reads are bounded by `DEFAULT_MAX_CONTENT_BYTES = 1_048_576` per document and `limit` rows per pass (default 1000). Oversized content is truncated/skipped per the bounded-read policy.

### Deterministic Replay Timestamp

- The importer does **not** read the host wall clock. When `ingest_timestamp` is omitted it defaults to `0.0`, the deterministic replay value. A caller may supply an explicit non-negative `ingest_timestamp` for wall-clock provenance; it is never inferred from the host during an offline rebuild.

### Untrusted Authority

- MasterDocsRAG content is **advisory evidence, not authority**. Imported records carry `authority="untrusted"` provenance and cannot grant permissions, bypass Verdict gates, or change routing/policy. Caller-supplied authority is metadata, not proof.
- Destination writes go to a caller-owned `MemoryPlane`; the adapter does not open or merge private third-party databases and does not deduplicate against unknown external stores.

### Dry-Run and CLI Usage

- The CLI subcommand `verdict memory masterdocs --db <path>` is the operator entry point. Flags:
  - `--allow-legacy-sqlite`: explicitly opt into a local exported SQLite artifact (prefer validated manifests).
  - `--dry-run`: canonicalize and emit the report without writing any records to the `MemoryPlane` (the default reporting path before commit).
  - `--limit <N>`: maximum source rows to inspect (default 1000).
  - `--ingest-timestamp <float>`: stable provenance timestamp; defaults to the deterministic zero value.
  - `--json`: emit machine-readable JSON for the canonical bundle.
- In dry-run mode the CLI prints the `MasterDocsIngestionReport` (or full JSON bundle with `--json`) and raises `SystemExit(1)` on `unavailable`, `rejected`, or `empty` status without writing to the plane. A non-dry-run pass imports via `canonicalize_db` and raises `SystemExit(1)` if the import is `rejected`/`partial` with zero records written.
- This is a **manifest-first** flow: operators should prefer a validated exported manifest over a raw SQLite file. The raw-SQLite path is an explicit, audited escape hatch, not the default authority.

---

## Status and Coverage

The MasterDocs canonical importer is delivered incrementally for issue #127; it is not a complete migration of all legacy stores. Capability and coverage status is reported by the default registry as available, degraded, unavailable, or unknown. Receipts, semantic indexing, and runtime daemon ownership remain follow-up slices (#117, #121, #129).
