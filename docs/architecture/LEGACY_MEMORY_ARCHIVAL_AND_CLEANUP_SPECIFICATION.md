# Legacy Memory Archival, Ingestion, and Erasure Specification

## Overview

To achieve a 100% local-first, zero-external-dependency memory architecture, Verdict completely eliminates legacy ununified memory systems, specifically **OpenViking**, legacy RAG JSON files, and uncoordinated local databases.

This specification details the transactional pipeline for archiving legacy memory artifacts, ingesting their records into the unified Verdict `MemoryPlane` (`.verdict/memory.db`), and safely erasing legacy files.

---

## Targeted Legacy Artifacts

The migration module (`verdict/memory_migration.py`) scans for and targets:
1. **OpenViking Stores**: `~/.openviking`, `.openviking/`, OpenViking SQLite DBs, and repair scripts (`repair-openviking-*.sh`).
2. **Legacy RAG Contexts**: `.pi_rag_context.json`, `MasterDocsRAG.db`.
3. **Legacy Code Graph DBs**: `.code-review-graph/`.
4. **Tool-Specific Memory Dumps**: `.codex-memory/`, uncoordinated `.claude-flow/memory/*.json`.

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
Removes the original legacy files and directories after verifying successful archival and ingestion. Ensures zero active code paths reference OpenViking or legacy RAG endpoints.
