# Spec Delta: Prime verified interactive selection

## Purpose

Provide exact, evidenced interactive model scope for Prime while keeping health, registry visibility, transport compatibility, and runtime launch admission separate.

## ADDED Requirements

### Requirement: Prime discovery and selection checks SHALL be read-only and independent

The system SHALL discover the Prime binary and resolved operator agent directory without creating files, syncing inventory, executing credential helpers, probing health, or invoking enable/certify. It SHALL show each candidate's exact route id, provider, observed projection status, checked_at, fresh_until, expires_at, coding_ok, and independent Prime visibility/execution-compatibility facts. Picker-specific Prime visibility SHALL be a separate overlay, not be injected into the projection to replace the observed health check. Visibility SHALL require exact membership in `models.json.providers.omniroute.models`, not gateway inventory membership. Transport compatibility SHALL require a supported exact-id API/endpoint binding, binary support and read-only credential readiness; it SHALL NOT claim a Prime end-to-end execution test from static configuration or gateway proof. Unknown readiness SHALL fail closed.

#### Scenario: Healthy but absent from the Prime registry
- **WHEN** `cx/gpt-6-sol` is VERIFIED in the projection but absent from the resolved Prime registry
- **THEN** the picker SHALL report `not visible to Prime`, refuse adding it, and offer inventory sync as a separate explicit step
- **AND** discovery and preview SHALL NOT sync or write any harness file

#### Scenario: Visible but unsupported transport
- **WHEN** a registry id has unsupported API mode, ambiguous exact resolution, missing binary, or unknown credential readiness
- **THEN** its execution fact SHALL be blocked or unknown with a reason, and the system SHALL NOT label it working or selectable

### Requirement: Selection SHALL use exact model ids and preserve identity

New managed `enabledModels` strings SHALL equal canonical projection ids and exact `providers.omniroute.models[].id` values. The observed raw gateway form `cx/gpt-6-sol` SHALL remain `cx/gpt-6-sol`; the system SHALL NOT guess names, collapse provider prefixes, infer models from suffixes, or invent a Prime provider named `cx`. Only the sibling canonical normalization of an explicit leading `omniroute/` SHALL be accepted before exact comparison. Wildcards, `auto/*`, ambiguous name matches, unsupported effort-suffix interpretations, and nonexact aliases SHALL be refused. A supported-release compatibility adapter SHALL validate that each literal token resolves only to the intended provider/id.

#### Scenario: Raw gateway id has an exact registry binding
- **WHEN** `cx/gpt-6-sol` is present as that exact id under Prime's omniroute provider and has supported unambiguous literal resolution
- **THEN** the proposed managed scope SHALL contain `cx/gpt-6-sol`, not a guessed display name or automatically prefixed alternate identity

#### Scenario: Auto or guessed model input
- **WHEN** input is `auto/*`, `sonnet`, or an id that would require fuzzy model-name resolution
- **THEN** selection SHALL refuse with syntax/identity help and SHALL NOT repair or apply another model

### Requirement: Only current verified compatible ids SHALL be added

A proposed added or retained managed id SHALL be selectable only with current projection status VERIFIED, identity verified, `now < fresh_until`, no applicable active selection blocker/cooldown, exact Prime visibility and supported compatibility. Missing timestamps, stale, unverified, failed, unavailable, excluded, inventory-only or incompatible evidence SHALL prevent apply as verified. A chat-only VERIFIED row SHALL display coding_ok=false without a hidden coding floor. Removal of an old managed id SHALL NOT require that id to be healthy. Cached proof SHALL NOT authorize launch.

#### Scenario: Freshness expires at the exact boundary
- **WHEN** confirmation or file preparation reaches the selected row's fresh_until timestamp
- **THEN** apply SHALL refuse before settings replacement, even if expires_at is later and the row was VERIFIED during preview

#### Scenario: Cooldown overrides a prior success
- **WHEN** a selected id has old positive proof but a current applicable cooldown
- **THEN** the system SHALL display the projection's blocked status and SHALL NOT apply it as working

#### Scenario: Chat-only verified scope
- **WHEN** a fresh compatible row is VERIFIED with coding_ok=false
- **THEN** the picker SHALL keep both facts visible and SHALL NOT silently require coding proof for this interactive scope

### Requirement: The target and scoped replacement policy SHALL be explicit

The only selection target SHALL be `/enabledModels` in the resolved operator global `settings.json`; no project or per-launch file SHALL be edited. The policy SHALL replace uniquely bound omniroute scope entries with the chosen nonempty exact list, deduplicate choices in first-selection order, preserve exact other-provider entries in relative order, and preserve unbound exact historical entries with `retained_unknown / not verified` warnings. Existing wildcard/ambiguous entries SHALL block safe apply rather than be expanded or silently removed. Other settings, defaultModel, defaultProvider, fallback fields, provider registry content and per-model metadata SHALL remain semantically unchanged. There SHALL be no default-model flag or hardcoded fallback in this story. A selected scope SHALL NOT be presented as a complete verified allowlist when preserved entries are unverified.

#### Scenario: Multi-id scope preserves unrelated state
- **WHEN** two exact verified ids replace an old omniroute scope alongside an exact other-provider entry
- **THEN** preview SHALL show both selections and removals, preserve the other-provider entry, and apply SHALL preserve all other settings and every models.json metadata value

#### Scenario: Historical unbound token stays honest
- **WHEN** the existing scope contains an exact token not bound in the current registry
- **THEN** preview SHALL show it as retained unknown, SHALL NOT label it verified, and SHALL NOT silently delete it

#### Scenario: Existing broad pattern prevents safe scope classification
- **WHEN** enabledModels contains a wildcard or ambiguous provider/name token
- **THEN** apply SHALL refuse and direct the owner to resolve that scope through Prime `/scoped-models`

### Requirement: Inventory sync SHALL remain a separate explicit operation

The picker SHALL NOT conflate `providers.omniroute.models` inventory sync with a verified selection. Missing visibility SHALL offer the existing sync action through a separate `verdict harness prime sync-models [--dry-run]` surface, with explicit write confirmation. Selection SHALL never implicitly call automatic visibility refresh, bootstrap a provider, overwrite another provider, or call enable. After a separate sync, the system SHALL re-discover and invalidate any old selection preview. Sync SHALL retain existing per-model metadata and SHALL make no working-model claim.

#### Scenario: Separate sync does not verify health
- **WHEN** an owner confirms an inventory-only sync after a visibility refusal
- **THEN** the newly visible id SHALL still require reloaded VERIFIED proof and compatible exact identity before selection, and the old preview SHALL be unusable

### Requirement: Picker consumers SHALL refresh involved ids then reload evidence

Picker preview and apply consumers SHALL call the sibling `refresh_for_consumer` for exactly proposed managed ids, wait with progress, and reload projection evidence before deciding. Fresh ids SHALL reuse proof without calls/wait. Only eligible prepaid routes SHALL refresh automatically. Metered/unknown refresh SHALL use a disclosed bounded manual plan with exact ids, count, request estimate, caps and quota warning and separate default-No spend consent. Refusal or incomplete/capped/cancelled/disabled/joined refresh SHALL NOT manufacture fresh proof or expand the selection. A valid confirmed METERED/UNKNOWN plan SHALL execute its exact disclosed ids through the sibling bounded plan/execute path; automatic prepaid filtering SHALL NOT discard these consented ids. An unconfirmed, expired/stale, changed or otherwise invalid plan SHALL refuse before calls. U7 SHALL depend on the unit-3 fix commit implementing this contract and SHALL NOT ship the baseline prepaid-only defect as a limitation, relabel capacity, fabricate proof or patch another unit's refresh module. After execution the picker SHALL re-read evidence, not infer verification from plan completion. A liveness-only refresh SHALL not imply coding or exact-identity verification; only reloaded projection evidence SHALL determine that. No refresh or live read SHALL occur inside the settings transaction lock. Pure transaction preview SHALL itself perform no refresh.

#### Scenario: Preview waits for prepaid evidence
- **WHEN** chosen prepaid ids need refresh
- **THEN** the consumer SHALL show progress only until bounded completion, reload evidence, and then generate the diff rather than displaying provisional working rows

#### Scenario: Metered spend is not implied by apply consent
- **WHEN** a metered or unknown id needs proof and the owner declines the spend plan
- **THEN** no metered calls SHALL occur, the id SHALL remain unselectable if unverified, and later settings confirmation SHALL NOT fabricate spend consent

#### Scenario: Confirmed metered and unknown ids actually execute
- **WHEN** a valid current digest-bound plan for exact METERED or UNKNOWN ids receives affirmative spend confirmation
- **THEN** the sibling bounded execute path SHALL send only the disclosed allowed requests for those ids rather than filtering them as prepaid-only
- **AND** the picker SHALL wait and re-read evidence before generating or applying a verified selection

#### Scenario: Unconfirmed or stale plan cannot probe metered ids
- **WHEN** the metered/unknown plan is unconfirmed, expired/stale or changed
- **THEN** execute SHALL refuse with zero model calls and the picker SHALL not treat consent or plan text as evidence

#### Scenario: Partial refresh does not widen selection
- **WHEN** the coordinator cap or cancellation leaves one chosen id stale
- **THEN** verified apply of the original set SHALL refuse without silently dropping that id or using another fallback

### Requirement: Selection previews SHALL be pure sanitized digest-bound plans

`preview_selection` SHALL consume a supplied SettingsDocument, projection rows, dependencies, exact selected ids and explicit time without reading disk/network/clock, locking, probing, writing, or creating backups. It SHALL expose only the target path/key, policy, scoped before/after, additions/removals, retained warnings, refusals, proof deadline, raw-byte pre-digest, dependency digests and plan digest. SHA256 SHALL bind the exact original settings bytes, not only parsed JSON. Registry/project/binary compatibility dependencies SHALL be bound and revalidated. Full-document diffs, credential values, raw backup content and secret-bearing reasons SHALL NOT be logged or rendered.

#### Scenario: Preview and cancellation do not mutate configuration
- **WHEN** an owner previews a valid multiple-id selection and then cancels
- **THEN** settings, registry, backups and transaction receipts SHALL remain unchanged, except independently completed authorized refresh evidence

#### Scenario: Unrelated secret setting is not displayed
- **WHEN** settings or registry contains credential values outside enabledModels
- **THEN** diff and error output SHALL show only sanitized scope changes/digests and SHALL NOT contain those values

### Requirement: Apply SHALL require affirmative consent and unchanged preimage

`apply_selection` SHALL require literal `confirmed=True`, expected_pre_digest matching the preview, and an unchanged preview plan. It SHALL acquire the nonblocking private `.verdict-prime-selection.lock` cross-process lock, re-read regular owner-controlled documents and file identity, reload local proof, and refuse if Prime is absent, dependencies/config changed, identity unsupported, or health expired. A changed set/diff or elapsed preview proof_deadline SHALL require a new preview and new confirmation, even if apply-time refresh obtains new proof; the approved plan deadline SHALL not be extended silently. Empty/n/EOF/Esc/Ctrl-C SHALL not apply. No force flag or silent merge SHALL bypass this contract. A currently identical scope SHALL be idempotent after validation, with no settings write or backup.

#### Scenario: Concurrent owner edit invalidates consent
- **WHEN** settings bytes change after the preview, including an unrelated key or whitespace change
- **THEN** apply SHALL return config_changed and SHALL NOT write settings or take an apply backup before detecting that mismatch

#### Scenario: Registry changes before apply
- **WHEN** a dependency registry/project digest or exact transport binding changes
- **THEN** apply SHALL refuse the old plan and require a fresh preview and confirmation

#### Scenario: Lock is held elsewhere
- **WHEN** another selection/restore transaction owns the lock
- **THEN** apply SHALL return busy without settings or backup mutation and SHALL NOT wait while launching a hidden refresh

#### Scenario: Repeated identical selection
- **WHEN** confirmed selection produces the already-present scoped array with current valid proof and unchanged dependencies
- **THEN** the result SHALL be unchanged and SHALL not create a redundant backup or replace settings

### Requirement: Confirmed writes SHALL be minimal atomic private recoverable transactions

Immediately before a non-no-op write, the locked current settings preimage SHALL be backed up byte-for-byte to `settings.json.verdict-select-<UTC stamp>-<transaction-id>.bak` using exclusive creation and mode 0600. Only the approved enabledModels semantic change SHALL be serialized, using a unique private same-directory temporary file, full write/fsync, final digest/file/freshness checks, atomic replace and directory fsync. Settings SHALL be 0600. A prepared private receipt SHALL bind pre/post digests, backup integrity and scoped before/after; successful writes SHALL finalize it. Incomplete receipt finalization after a write SHALL report a recoverable applied state rather than falsely claiming no change. Only the newest five completed selection-owned backup/receipt pairs SHALL be retained; incomplete/prepared/unmatched recovery records and all other backup families SHALL be preserved. Pruning SHALL run only after successful finalization.

#### Scenario: Immediate preimage and retained metadata
- **WHEN** a valid confirmed multi-id transaction succeeds
- **THEN** its 0600 backup SHALL equal the immediate old settings bytes, its post-digest SHALL match the new bytes, and unrelated settings/registry model metadata SHALL be unchanged

#### Scenario: Mutation during file preparation
- **WHEN** an external settings edit is observed before atomic replacement
- **THEN** the transaction SHALL abort replacement, preserve the later settings, and retain any valid recovery backup without claiming apply success

#### Scenario: Crash recovery has exact digest states
- **WHEN** a prepared receipt survives a crash
- **THEN** only current pre-digest or post-digest with intact backup SHALL identify safe not-applied or recoverable-applied states; any other digest SHALL refuse recovery

#### Scenario: Retention does not prune another operation
- **WHEN** six completed selection transactions exist alongside models.json sync/enable backups and incomplete recovery files
- **THEN** pruning SHALL keep the newest five completed selection pairs and SHALL leave the other families/recovery files untouched

### Requirement: Restore SHALL preview safely and refuse later edits

Restore preview SHALL be pure and display only the reversal of the scoped key plus safe-restore status. Confirmed restore SHALL require expected_post_digest from the applied receipt, validated backup bytes/path/ownership, and current settings digest exactly equal to that post-digest under the same lock and immediately before replacement. It SHALL refuse any later owner edit without a force mode. It SHALL take an immediate 0600 pre-restore backup, restore the validated preimage atomically byte-for-byte at 0600, preserve the original selection receipt/backup, and retain only five completed restore-owned pairs. A repeated already-recorded restore SHALL be unchanged only with matching recorded restored digest. Restored historic ids SHALL be labelled prior scope, not newly verified. Recovery SHALL NOT require historic ids to pass new selection health, and SHALL never be presented as launch permission.

#### Scenario: Later unrelated edit blocks rollback
- **WHEN** an owner changes any settings bytes after selection
- **THEN** restore SHALL show config_changed, preserve the later file, and refuse to overwrite it

#### Scenario: Confirmed exact rollback
- **WHEN** the post-apply digest and backup integrity match and restore is explicitly confirmed
- **THEN** the settings bytes SHALL equal the prior preimage, the pre-restore backup SHALL preserve current bytes, and output SHALL not claim historical model health

#### Scenario: Restore is idempotent after completion
- **WHEN** restore is repeated with its receipt and the current digest equals the recorded restored digest
- **THEN** restore SHALL return unchanged without a replacement or redundant backup

### Requirement: TUI and CLI scope SHALL remain interactive not launch authority

`/bootstrap prime [ids...]` and `verdict harness prime select [ids...]` SHALL use guided selection or the same preview/confirmation flow. `/bootstrap prime restore` and `verdict harness prime restore` SHALL use the guarded restore flow. `--preview` SHALL stop before settings mutation. Noninteractive EOF SHALL never imply confirmation; JSON mode SHALL emit one final envelope with progress/prompts on stderr. Help/docs SHALL explain that enabledModels affects interactive Alt+M cycling, `/scoped-models`, restart/override limits, separate sync, defaultModel preservation and restore safety. Verdict worker launches SHALL continue exact Verdict selection plus runtime admission/reconfirmation; no cache/scope or fallback SHALL bypass it. Existing harness certification SHALL remain unchanged.

#### Scenario: New interactive scope is not a worker allowlist
- **WHEN** an owner applies a Prime interactive selection and later dispatches a Verdict worker
- **THEN** dispatch SHALL still use Verdict's exact selected id and current runtime admission/confirmation, not trust enabledModels as permission

#### Scenario: Preview-only JSON invocation
- **WHEN** `verdict harness prime select <ids> --preview --json` runs without interactive confirmation
- **THEN** it SHALL return a sanitized preview and SHALL not write settings, backups or receipts
