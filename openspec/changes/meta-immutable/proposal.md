### Proposal: MetadataSnapshot fields are read-only

## Why

`MetadataSnapshot.sources` and `MetadataSnapshot.mapping` are plain mutable
`dict` fields inside a `@dataclass(frozen=True)`.  Frozen prevents replacing
the field, but not mutating the dict in place.

Since PR #1268, `load_store()` returns a cached snapshot
(`_load_store_cached`, `lru_cache(maxsize=4)`) shared by every caller.  A
caller that mutates `snapshot.mapping[key] = value` or
`snapshot.sources[key] = ...` corrupts the shared cache for every other caller
in the same process — silently, with no TypeError.

The fix wraps both fields in `types.MappingProxyType` during `__post_init__`.
The proxy is read-only: any `__setitem__` raises `TypeError`.  The dict is
copied before wrapping so the caller's input dict cannot be mutated through the
proxy after construction.

**Evidence boundary**

- KNOWN: `MetadataSnapshot` is `@dataclass(frozen=True)` in
  `verdict/metadata/store.py`; `sources` and `mapping` are declared `dict`.
- KNOWN: `_load_store_cached` in the same file uses
  `@lru_cache(maxsize=4)`, making every returned snapshot a shared object.
- KNOWN: `MappingProxyType` raises `TypeError` on `__setitem__`.
- KNOWN: `json.dumps` does not serialise `MappingProxyType`; callers that
  serialise `snapshot.mapping` directly need `dict(snapshot.mapping)`.
- INFERRED: no current test mutates `snapshot.sources` or `snapshot.mapping`
  after construction, so the change is safe for existing callers.
- NOT AVAILABLE: confirmation that the hourly acceptance checks continue to
  pass on installed main after the merge.

## What Changes

- **`verdict/metadata/store.py`**: `sources` annotation changed from
  `dict[str, SourceStatus]` to `Mapping[str, SourceStatus]`; `mapping`
  annotation changed from `dict[str, Any]` to `Mapping[str, Any]`.
  `__post_init__` wraps both via `object.__setattr__` with
  `MappingProxyType(dict(...))` so the fields are read-only after
  construction.
- **`verdict/actions/extra.py`**: the one inline serialisation of
  `snapshot.mapping` is changed to `dict(snapshot.mapping)` so `json.dumps`
  continues to work.
- **`tests/test_model_metadata.py`**: three new tests in `TestStoreAndSchema`:
  `test_sources_field_is_read_only`, `test_mapping_field_is_read_only`, and
  `test_load_store_returns_same_cached_object`.

## Capabilities

### Modified Capabilities

- **MetadataSnapshot**: `sources` and `mapping` fields are now read-only proxies.
  Callers that need a mutable copy must call `dict(snapshot.sources)` or
  `dict(snapshot.mapping)`.

## Impact

Affected files: `verdict/metadata/store.py`, `verdict/actions/extra.py`,
`tests/test_model_metadata.py`.

No change to the data model, wire format, or any public API.  The `to_dict()`
method already called `dict(self.mapping)` and continues to work unchanged.

## Acceptance criteria

- Calling `snapshot.sources["key"] = value` on a loaded snapshot raises
  `TypeError`.
- Calling `snapshot.mapping["key"] = value` on a loaded snapshot raises
  `TypeError`.
- Two successive `load_store(path)` calls for the same unmodified file return
  the same object (`is`).
- The full test suite passes; `test_real_inventory_to_view_and_render_under_1s`
  stays under its time limit.
