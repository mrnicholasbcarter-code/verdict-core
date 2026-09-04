# Data Model: Context Intelligence Lift

## RetrievalSlice

| Field | Rule |
|-------|------|
| `slice_id` | Stable id |
| `category` | `docs` / `code` / `memory` |
| `query` | Bounded lookup text; not a directory dump |
| `root` | Optional default location (`docs/adr`, proof root) |
| `max_units` | Hard cap; exceeding is omit, not dump |

Slices that would select a whole repository are illegal.

## Context unit (reuse `ContextUnit`)

Existing compiler unit. Slot mapping for this feature:

| Product slot | `slot_type` | `key` prefix |
|--------------|-------------|--------------|
| goal | `instructions` | `goal` |
| policy | `policy` | `policy` |
| docs | `evidence` | `docs:` |
| code | `evidence` | `code:` |
| memory | `memory` | `memory:` |

Each unit keeps `source_uri`, `source_digest`, `observed_at`, `trust`.

## Omission

| Field | Rule |
|-------|------|
| `category` | Source category |
| `reason` | `not_found` / `no_default_location` / `budget` / `unsafe` / `stale` / `refused_slice` |
| `ref` | Optional source pointer |

Silent drop is illegal.

## WorkingState

Typed slots for one task. Not a transcript.

| Slot | Content |
|------|---------|
| `goal` | Task text |
| `slices` | Planned slice ids |
| `pack_digest` | Digest after compile, if any |
| `required_fact_kept` | Boolean |
| `omissions` | Omission list |

Must not automatically ingest into durable memory.

## Durable memory record (reuse `MemoryRecord`)

Gated via `MemoryGate`. Search via `MemoryPlane.search_ranked`. Stale when age exceeds declared TTL (fail open if no TTL). Secrets and transcripts never persist as retrievable content.

## Context pack (reuse `ContextPack` + `ContextReceipt`)

Compiled, budgeted rendering. Receipt is payload-free: decisions, token use vs budget, conflicts, omissions, pack digest. No secrets.

## Named check

| Field | Rule |
|-------|------|
| `planted_token` | Synthetic non-secret unique string |
| `artifact` | Exactly `{"lift_fact":"<planted_token>"}` |
| `checker` | JSON parse; equality on `lift_fact` |

Token must not appear in the unaided prompt.

## PairedLiftReceipt

| Field | Rule |
|-------|------|
| `schema_version` | `context-intelligence/v1` |
| `identity_id` | Same cheaper identity for both attempts |
| `cost_class` | `local` / `free` / `cheaper` (never paid while cheaper unused remains) |
| `endpoint` | Live gateway |
| `pack_digest` | Digest of packed attempt’s pack |
| `unaided_passed` | Checker result |
| `packed_passed` | Checker result |
| `conclusion` | `lift` / `no_lift` / `blocked` |
| `block_reason` | Required when blocked |
| `omissions` | Named omissions |

Invariant: `lift` only if unaided failed and packed succeeded. Identities must match. No secrets, prompts, or completions on the receipt.

## Cheaper identity

Reuse Feature 276 `ConcreteIdentity`. Lift subject requires fetched `cost_class` in `{local,free,cheaper}` and fetched `context_limit`. Unknown context limit → unclassified for packing → blocked live proof.
