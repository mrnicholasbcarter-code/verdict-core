# Tasks

## 1. Normalised-Leaf Utilities

- [x] 1.1 Add `_normalize_leaf_version(leaf: str) -> str` to `verdict/metadata/store.py`
  — replaces digit-dash-digit with digit-dot-digit; alpha-dash-digit is left unchanged.
- [x] 1.2 Add `all_records_normalized_leaf_index(snapshot)` to `verdict/metadata/store.py`
  — builds a dict keyed by normalised leaf; disambiguation: dot-form (raw leaf ==
  normalised leaf) wins over its dash twin; ambiguous pairs stored as sorted tuple.
- [x] 1.3 Add `MetadataSnapshot._normalized_leaf_index` cached property using
  `all_records_normalized_leaf_index`.

## 2. Join Provenance Types and Fields

- [x] 2.1 Add `LeafJoin = Literal["explicit", "leaf"]` type alias to
  `verdict/metadata/records.py`.
- [x] 2.2 Add `join: LeafJoin | None = None` field to `MetadataLookup` dataclass.
- [x] 2.3 Update `MetadataLookup.to_dict()` to include `"join"` when not `None`.
- [x] 2.4 Add `join: LeafJoin | None = None` field to `RatedModel` dataclass in
  `verdict/identify_rank.py`.

## 3. Lookup Integration

- [x] 3.1 Update `lookup_omniroute_id` in `verdict/metadata/store.py`:
  - [x] 3.1.1 Track `join` provenance variable (starts `None`).
  - [x] 3.1.2 Set `join = "explicit"` for explicit map hits and OmniRoute-id index hits.
  - [x] 3.1.3 Add third-tier normalised-leaf lookup: strip provider prefix, normalise
    version separators, look up in `_normalized_leaf_index`.
  - [x] 3.1.4 If the index returns a `ModelMetadataRecord`, set `models_dev_id` and
    `join = "leaf"`.
  - [x] 3.1.5 If the index returns a tuple (ambiguous), return `MetadataLookup` with
    `drop.reason = DROP_UNMAPPED` and `detail` mentioning "ambiguous normalised leaf".
  - [x] 3.1.6 Propagate `join` to the returned `MetadataLookup`.

## 4. Rating Propagation

- [x] 4.1 Update `rate_model` in `verdict/identify_rank.py` to capture `lookup.join`
  and pass it to `RatedModel(join=...)`.
- [x] 4.2 Add `"rating_join": rating.join` to `_rank_components` dict in
  `verdict/orchestration/eligibility.py`.
- [x] 4.3 Add `"rating_join": components.get("rating_join")` to
  `_rank_components_for_display`.

## 5. Tests

- [x] 5.1 Update `tests/test_identify_rank.py`:
  - [x] 5.1.1 Update `TestSnapshotLookupIndexes.test_repeated_lookups_keep_join_semantics`
    to verify `_normalized_leaf_index` is built once (identity check) instead of
    counting `_record_from_models_json` calls.
  - [x] 5.1.2 Rename `test_dot_spelled_inventory_id_is_not_normalised` →
    `test_dot_spelled_inventory_id_resolves_via_leaf_join`; assert the dot-spelled
    route resolves with `join == "leaf"`.
  - [x] 5.1.3 Update `TestShippedMapJoinsDotIdRatings` docstring to remove the
    "No dash/dot normaliser may produce the join" constraint.
  - [x] 5.1.4 Add `assert found.join == "explicit"` and `assert rated.join == "explicit"`
    to `test_four_claude_5_5_routes_resolve_to_the_scored_record`.
- [x] 5.2 Add new tests to `tests/test_model_metadata.py::TestMappingAndDrops`:
  - [x] 5.2.1 `test_normalized_leaf_join_dash_to_dot_record`: dash-spelled inventory id
    resolves to dot record; `join == "leaf"`.
  - [x] 5.2.2 `test_normalized_leaf_join_provenance_is_leaf`: leaf join → `join=="leaf"`;
    explicit map → `join=="explicit"`.
  - [x] 5.2.3 `test_ambiguous_normalized_leaf_is_named_drop`: two distinct-vendor models
    with the same normalised leaf → `DROP_UNMAPPED`, detail mentions "ambiguous".
  - [x] 5.2.4 `test_no_digit_dash_digit_stays_unmapped_without_match`: alpha-dash-digit
    tail route stays unmapped when no matching store record exists.

## 6. Acceptance

- [x] 6.1 `uv run python -m pytest tests/test_identify_rank.py tests/test_model_metadata.py -q` (0 failed)
- [x] 6.2 `uv run ruff check .` (exit 0)
- [x] 6.3 `uv run ruff format --check .` (exit 0)
- [x] 6.4 `uv run mypy verdict --strict` (exit 0)
- [x] 6.5 `uv run python scripts/check_lint_gates.py` (exit 0)
- [x] 6.6 `uv run python scripts/check_reachability.py --ratchet-base origin/main` (no new findings)
- [x] 6.7 `verdict openspec admit ratings-leaf-join` (exit 0)
