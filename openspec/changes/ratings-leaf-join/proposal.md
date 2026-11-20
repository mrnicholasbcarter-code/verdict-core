# Proposal: Normalised-leaf join for OmniRoute → metadata records

## Why

OmniRoute inventory IDs use a dash convention for version separators
(`cc/claude-opus-5-5`), while the OpenRouter Artificial Analysis mirror rows
in the models.dev store use a dot convention (`anthropic/claude-opus-5.5`).
The models.dev `models.json` feed produces a dash record for each such model
with no AA scores attached; the `api.json` feed produces the dot record and
carries the scores.  The explicit identity map already joins the four known
Claude 5.5 family routes, but new dot-score models would silently miss
ratings until manually added to the map.

This change adds an automatic normalised-leaf index as the final fallback in
`lookup_omniroute_id`, after the explicit map and the existing OmniRoute-id
index.  The join provenance (`"explicit"` or `"leaf"`) is recorded on
`MetadataLookup` and propagated through `RatedModel` and `_rank_components`
so the receipt shows how each rating was matched.

## What Changes

- `verdict/metadata/store.py`: add `_normalize_leaf_version` (digit-dash-digit
  → digit-dot-digit), add `all_records_normalized_leaf_index` (indexes ALL
  store records; disambiguation: dot-form wins over dash twin; else ambiguous
  tuple), add `MetadataSnapshot._normalized_leaf_index` cached property,
  update `lookup_omniroute_id` to use the new index as a third-tier fallback
  and record `join` provenance on the returned `MetadataLookup`.
- `verdict/metadata/records.py`: add `LeafJoin = Literal["explicit", "leaf"]`
  type alias, add `join: LeafJoin | None` field to `MetadataLookup`, update
  `to_dict` to include it when set.
- `verdict/identify_rank.py`: add `join: LeafJoin | None` field to
  `RatedModel`, propagate from `lookup_omniroute_id` result.
- `verdict/orchestration/eligibility.py`: add `"rating_join"` key to
  `_rank_components` and `_rank_components_for_display`.

## Capabilities

### Modified Capabilities

- `metadata/lookup`: `lookup_omniroute_id` gains a third join tier
  (normalised-leaf index).  Existing explicit-map and OmniRoute-id tiers are
  unchanged.  Ambiguous normalised leaves (two distinct models share the same
  normalised leaf) produce `DROP_UNMAPPED` as before.
- `metadata/records`: `MetadataLookup` gains a `join` field
  (`"explicit"` | `"leaf"` | `None`).
- `identify-rank`: `RatedModel` gains a `join` field.
- `orchestration/eligibility`: `rank_components` and the display view gain a
  `rating_join` key.

## Acceptance criteria

- [x] `cc/claude-opus-5-5` resolves to `anthropic/claude-opus-5.5` via
  normalised-leaf join when no explicit map entry is present; `join == "leaf"`.
- [x] Explicit map entry wins over leaf join; `join == "explicit"` for mapped
  routes.
- [x] `cc/claude-opus-5.5` (dot-spelled, no explicit map entry) resolves via
  leaf join to `anthropic/claude-opus-5.5`.
- [x] `cc/claude-fable-5` (alpha-dash-digit tail, no digit-dash-digit) stays
  unmapped when no matching store record exists.
- [x] Two different models from different vendors with the same normalised leaf
  produce `DROP_UNMAPPED` with detail mentioning "ambiguous".
- [x] `all_records_normalized_leaf_index` returns a singleton for a unique
  normalised leaf, and a tuple (sorted record ids) for an ambiguous one.
- [x] `MetadataSnapshot._normalized_leaf_index` is built once per snapshot
  instance (`@cached_property`).
- [x] `MetadataLookup.join` is included in `to_dict()` when not `None`.
- [x] `RatedModel.join` propagates the lookup join provenance.
- [x] `rank_components["rating_join"]` and the display view include the join
  field.
- [x] New tests pass, full suite 0 failed.
- [x] `mypy --strict verdict` (exit 0).
- [x] `ruff check .` and `ruff format --check .` (exit 0).
- [x] `verdict openspec admit ratings-leaf-join` (exit 0).

## Impact

Affected modules:
- `verdict/metadata/store.py` — normalised-leaf index and join logic
- `verdict/metadata/records.py` — `LeafJoin` type alias and `MetadataLookup.join`
- `verdict/identify_rank.py` — `RatedModel.join`
- `verdict/orchestration/eligibility.py` — `_rank_components` / `_rank_components_for_display`

No new dependencies.  No network calls added.  No schema version bump required
(the `join` field is added as an optional key in `to_dict`, which the existing
schema treats as extra properties).
