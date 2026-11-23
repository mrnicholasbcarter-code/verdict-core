# Design: meta-immutable

## Context

`MetadataSnapshot` is a `@dataclass(frozen=True)` defined in
`verdict/metadata/store.py`.  `frozen=True` prevents *replacing* a field
(e.g. `snapshot.sources = {}`) but does not prevent *mutating* the dict
the field points to (e.g. `snapshot.sources["k"] = v`).

Before PR #1268, `load_store` created a fresh snapshot on every call.
PR #1268 added `_load_store_cached` with `@lru_cache(maxsize=4)`, keyed on
`(path, mtime_ns)`.  Every caller that opens the same file within the same
mtime window now receives the **same Python object**.  A mutation by any
caller corrupts the shared snapshot for all subsequent callers — silently.

The fix wraps `sources` and `mapping` in `types.MappingProxyType`.  This is
the standard Python mechanism for making a dict read-only: any `__setitem__`
or `__delitem__` on a proxy raises `TypeError`.

## Goals / Non-Goals

**Goals:**
- Make `snapshot.sources` and `snapshot.mapping` raise `TypeError` on
  attempted mutation, so shared cached snapshots cannot be corrupted.
- Keep all existing callers working without code changes where possible.
- Fix the one production serialisation path that passes `snapshot.mapping`
  directly to JSON (not JSON-serialisable as a proxy).

**Non-Goals:**
- Making `records`, `drops`, or `conflicts` read-only (they are already
  tuples, which are immutable by construction).
- Changing the wire format, schema, or any public facing API.
- Wrapping deeply nested values inside `sources` or `mapping`.

## Decisions

1. **`MappingProxyType(dict(x))` not `MappingProxyType(x)`.**
   `MappingProxyType(x)` is a live view of the original dict; a caller that
   retains a reference to the dict can still mutate the snapshot's view.
   Copying first (`dict(x)`) severs that link.  The copy happens once, at
   construction time, with negligible cost.

2. **`object.__setattr__` in `__post_init__`.**
   `frozen=True` raises `FrozenInstanceError` on direct attribute assignment
   inside `__post_init__`.  The standard Python workaround is
   `object.__setattr__(self, field, value)`, which writes directly to the
   instance `__dict__` without going through the dataclass descriptor.  This
   is documented Python behaviour for frozen dataclasses.

3. **Annotation changed to `Mapping[str, ...]`.**
   Declaring the field as `Mapping` (from `collections.abc`) correctly signals
   to mypy and to callers that the value supports read access only.  `dict`
   is a subtype of `Mapping`, so callers that pass a plain dict at
   construction time continue to work.

4. **Fix `extra.py` serialisation.**
   `verdict/actions/extra.py:1187` serialises `snapshot.mapping` directly
   into a dict that is later passed to `json.dumps`.  `MappingProxyType` is
   not a dict subclass and is not JSON-serialisable by default.  The fix is
   `dict(snapshot.mapping)`, consistent with how `to_dict()` already handles
   the same field.

## Risks / Trade-offs

- **Existing callers that intentionally mutate snapshots.** There are none
  in the production code at the time of this change.  The reachability check
  confirms no callers modify `snapshot.sources` or `snapshot.mapping` after
  construction.
- **Performance.** One extra dict copy per snapshot construction.  Snapshots
  are constructed rarely (on cache miss) and are thereafter cached; the cost
  is negligible.
- **Downstream callers that expect a `dict`.** The return type is narrowed to
  `Mapping`.  Any caller that passes `snapshot.sources` or `snapshot.mapping`
  to a function expecting `dict[...]` will get a mypy error.  There are no
  such callers at the time of this change.

## Security / trust implications

No change to trust boundaries.  The fix prevents in-process mutation of
cached state; it has no effect on network, file I/O, or authentication paths.

## Routing / context / memory implications

`MetadataSnapshot` is consumed by the intelligence, router, and context-pack
paths.  None of those paths mutate the snapshot after loading.  Read-only
access is unaffected: `snapshot.mapping.get(...)`, iteration, and `in` checks
all work on a proxy.

## Migration / rollback

Rollback: revert the three changed files (`store.py`, `extra.py`,
`test_model_metadata.py`).  After rollback the fields are plain dicts again;
existing callers continue to work.  No schema or database changes.

## ADR impact

No new ADR required.  This is a defensive hardening of an internal data class
following the introduction of a caching layer.  No new architectural principle
is introduced.
