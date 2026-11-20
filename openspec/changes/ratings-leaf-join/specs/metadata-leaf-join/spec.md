# Spec Delta: Normalised-leaf join for OmniRoute-to-metadata lookup

## Purpose

Extend `lookup_omniroute_id` with a third join tier so that dash-spelled
OmniRoute inventory ids (e.g. `cc/claude-opus-5-5`) automatically resolve to
their dot-spelled store counterparts (e.g. `anthropic/claude-opus-5.5`) when
no explicit map entry or omniroute_id index hit is present.  The explicit map
remains the highest-priority path and always wins.

## ADDED Requirements

### Requirement: lookup_omniroute_id SHALL resolve dash-spelled inventory ids to dot-record counterparts via the normalised-leaf index

The system SHALL maintain a normalised-leaf index built from models.json records
only (consistent with the existing raw-leaf index).  Normalisation converts
digit-dash-digit sequences to digit-dot-digit (e.g. `claude-opus-5-5` becomes
`claude-opus-5.5`).  When the explicit map and the omniroute_id index both miss,
the system SHALL strip the provider prefix from the inventory id, apply the same
normalisation, and look up the result in the models.json normalised-leaf index.
A unique models.json match SHALL resolve the lookup.  If the matched record is
in dash form (raw leaf differs from the normalised leaf), the system SHALL
attempt a dot-twin upgrade: check for a record keyed `{provider}/{norm_leaf}` in
the full store and use it if found, so that the scored api.json dot record is
returned in preference to the unscored models.json dash record.  The explicit
map takes precedence: when an explicit map entry is present, the normalised-leaf
index SHALL NOT be consulted.

#### Scenario: Dash-spelled inventory id resolves to dot record
- **WHEN** `cc/claude-opus-5-5` is looked up and no explicit map entry is present
- **AND** the store contains `anthropic/claude-opus-5-5` (no scores) and `anthropic/claude-opus-5.5` (with scores)
- **THEN** the lookup SHALL return the `anthropic/claude-opus-5.5` record
- **AND** `MetadataLookup.join` SHALL be `"leaf"`

#### Scenario: Explicit map wins over normalised-leaf index
- **WHEN** `cc/claude-opus-5-5` is looked up with an explicit map entry pointing to `anthropic/claude-opus-5.5`
- **THEN** the lookup SHALL follow the explicit map
- **AND** `MetadataLookup.join` SHALL be `"explicit"`

#### Scenario: Dot-spelled inventory id resolves via leaf index when no map entry exists
- **WHEN** `cc/claude-opus-5.5` is looked up and no explicit map entry is present
- **AND** the store contains `anthropic/claude-opus-5.5`
- **THEN** the lookup SHALL return the `anthropic/claude-opus-5.5` record
- **AND** `MetadataLookup.join` SHALL be `"leaf"`

### Requirement: Ambiguous normalised leaves SHALL produce a named drop, never an arbitrary pick

When two or more distinct models from different vendors share the same normalised
leaf, the system SHALL produce a `DROP_UNMAPPED` result with a detail string
that includes the word "ambiguous".  The dot-form disambiguation (preferring
records whose raw leaf equals the normalised leaf) resolves only within the
same underlying model (dash-vs-dot twins); it SHALL NOT resolve collisions
between genuinely different models.

#### Scenario: Two different-vendor models share the same leaf
- **WHEN** `agy/shared-leaf` is looked up
- **AND** the store contains `google/shared-leaf` and `anthropic/shared-leaf` as separate models
- **THEN** the lookup SHALL return `drop.reason == DROP_UNMAPPED`
- **AND** `drop.detail` SHALL contain "ambiguous"
- **AND** `record` SHALL be `None`

#### Scenario: Alpha-dash-digit tail is not normalised
- **WHEN** `cc/claude-fable-5` is looked up and no store record matches that leaf
- **THEN** the lookup SHALL return `drop.reason == DROP_UNMAPPED`
- **AND** no normalisation SHALL bridge `claude-fable-5` to any scored record

### Requirement: Join provenance SHALL be recorded and propagated

The system SHALL record how each `lookup_omniroute_id` call resolved on the
returned `MetadataLookup` object via its `join` field (`"explicit"` for the
map/omniroute-id paths, `"leaf"` for the normalised-leaf path, `None` when
no record was found).  This provenance SHALL propagate through `RatedModel.join`
and appear as `rating_join` in `rank_components` and its display view.

#### Scenario: Rank components include rating_join
- **WHEN** a route is rated and selected via `EligibilityLadder`
- **THEN** `verdict.rank_components["rating_join"]` SHALL be `"explicit"`, `"leaf"`, or `None`
- **AND** the display view returned by `_rank_components_for_display` SHALL include `rating_join`
