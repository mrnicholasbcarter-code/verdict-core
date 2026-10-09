# Spec Delta: Local context-aware command completion

## Purpose

Extend the existing prompt_toolkit command prompt with bounded command/argument suggestions, syntax and typo help using only frozen local data, without side effects or live work on keystrokes.

## ADDED Requirements

### Requirement: Completion SHALL extend the existing prompt_toolkit path with a pure core

The system SHALL use the existing prompt_toolkit Completer/Completion framework, not introduce another prompt framework. A new pure completion core SHALL accept text_before_cursor, immutable CommandSpec metadata, a CompletionSnapshot, explicit time and a bounded limit, returning deterministic CompletionCandidate values with text, start_position, display, description and kind. The actual VerdictCompleter adapter SHALL consume prompt_toolkit Document/CompleteEvent and yield Completion values with correct token replacement offsets. The core SHALL not import home.py or action/refresh/admission adapters or perform clock/disk/network reads or writes.

#### Scenario: Actual completer uses frozen inputs
- **WHEN** a prompt_toolkit Document for `/bootstrap prime cc/` is completed
- **THEN** VerdictCompleter SHALL yield only bounded local exact-id candidates with token-correct offsets and metadata, without invoking actions or reading files

#### Scenario: Cursor is inside a line
- **WHEN** completion is requested before the end of a partially typed command line
- **THEN** replacements SHALL cover only the applicable token before the cursor and SHALL preserve unrelated arguments/text

### Requirement: Slash command suggestions SHALL include descriptions fuzzy matches and typo hints

The command grammar SHALL include every existing palette command and builtin, verified `/eligibility` and `refresh` syntax, `/probe`, and the new `/bootstrap` surfaces. Commands SHALL have descriptions and syntax metadata. Prefix and substring matches SHALL rank before bounded fuzzy subsequence/edit-distance suggestions, with stable lexical tie-breaks. Typo recovery SHALL apply to command/choice grammar only and SHALL never auto-repair a model id or auto-execute a command.

#### Scenario: Typo suggests the correct command
- **WHEN** `/elegibility` is typed or submitted
- **THEN** completion/help SHALL offer `/eligibility` with its description and SHALL not execute eligibility or run refresh until the owner explicitly submits the corrected command

#### Scenario: Partial or fuzzy command input
- **WHEN** `/boot` or a supported fuzzy abbreviation of bootstrap is completed
- **THEN** `/bootstrap` SHALL appear with description and syntax within the bounded results, preserving all existing palette entries

### Requirement: Fields flags and choices SHALL have unambiguous help

One shared grammar SHALL describe positional/prompted fields, flags, choices and secret arguments. TUI `/eligibility` SHALL show `[status] [provider=..] [search=..] [page=..] [refresh]` while CLI equivalents use `--flags`. `/probe <ids>` and `/bootstrap prime [ids...]` SHALL identify model fields as exact lists, not generic inline CLI text. Tokens beginning `--` in model fields SHALL be rejected with syntax help before consent or dispatch; `--help` SHALL display help, not become an id. No unsupported `--probe` token SHALL be interpreted as a model. Bootstrap target/mode and restore syntax SHALL be documented consistently in both prompt paths.

#### Scenario: CLI flag pasted into a TUI model field
- **WHEN** `/probe --probe` or `/bootstrap prime --probe` is submitted
- **THEN** the prompt SHALL explain that --probe is not a model id and SHALL make no probe/refresh/apply calls

#### Scenario: Eligibility field syntax is explicit
- **WHEN** the owner requests `/help eligibility` or completion after `/eligibility `
- **THEN** syntax SHALL distinguish status/provider=/search=/page=/refresh from CLI flags and SHALL not prompt for a model id or hidden scope

### Requirement: Argument candidates SHALL come only from frozen local snapshots

Completion SHALL use already-loaded local run ids and a sanitized local projection snapshot for exact model ids, statuses/providers and freshness labels. Harness targets SHALL be the static choices prime and claude; supported mode help SHALL state Prime interactive scope and Claude native/openai-side-path report modes. Eligibility key/value candidates SHALL come from the shared grammar and cached provider/model/page metadata, not live queries. The completer SHALL not fetch inventory, call models.verified, start/join refresh, wait, probe, execute a credential helper, or read files per keystroke, even when another job is running or auto-refresh is enabled. The snapshot loader SHALL run only on prompt entry or after an explicit command, outside completion.

#### Scenario: Model and harness arguments are local
- **WHEN** `/bootstrap `, `/bootstrap claude mode=`, `/probe cc/`, or `/eligibility provider=` is completed
- **THEN** suggestions SHALL use only prime/claude, supported report modes, exact locally cached model ids or cached provider names for that context

#### Scenario: Slash run command arguments work
- **WHEN** `/trace run-` is completed with loaded local run ids
- **THEN** matching run ids SHALL be suggested despite the leading slash, with no run/network discovery per Tab

#### Scenario: In-flight refresh does not block completion
- **WHEN** stale snapshot models exist and a refresh job is active
- **THEN** completion SHALL return immediately from local data without joining the job or waiting for proof

### Requirement: Completion snapshots SHALL be sanitized bounded disposable convenience state

After an explicit verified-view/picker consumer finishes refresh/projection, a separate post-render publisher SHALL write `~/.verdict/verified-completion-snapshot.json` using `verdict.completion-snapshot/v1`, mode 0600 and unique atomic replacement. It SHALL contain only generation/time, provider names, source errors and safe projection identity/status/timestamp metadata, never credentials, raw error bodies, account bindings, URLs or settings. It SHALL gather consistent local projection pages outside completion without live fetches and SHALL not treat one displayed page as the full catalog. Input SHALL be capped at 10,000 model rows and 4 MiB, with deterministic truncation and incomplete warning; loaded run rows SHALL be capped at 200. The projection/read action SHALL remain read-only and snapshot publication SHALL not write health, visibility, receipt or admission evidence. Missing/corrupt/unsupported/future-dated/oversize snapshots SHALL yield no model candidates plus local-snapshot help, never a live fallback.

#### Scenario: Missing snapshot stays offline
- **WHEN** the local snapshot is missing or invalid
- **THEN** commands/static grammar SHALL still complete, model suggestions SHALL be empty, and help SHALL say to run `/eligibility` explicitly rather than fetching live

#### Scenario: Full local catalog coverage is separate from paging
- **WHEN** a verified view displays one 50-row page of a 6,773-row local projection
- **THEN** post-command publication SHALL obtain consistent bounded local pages for completion without model calls, and Tab SHALL not perform publication or page reads

#### Scenario: Snapshot write failure is not evidence failure
- **WHEN** sanitized snapshot publication fails
- **THEN** the owner SHALL receive a warning while verification/admission facts remain unchanged and completion SHALL continue from the previous valid snapshot or static help

### Requirement: Stale and unverified model candidates SHALL be labelled and never become authority

Candidate display SHALL preserve projection status and provider, checked_at/fresh_until/expiry where available, and distinguish VERIFIED snapshot, STALE snapshot, UNVERIFIED and other blocked states. A stored VERIFIED row at or after fresh_until SHALL be shown as stale/recheck-required. Unknown or missing proof SHALL not become working. Exact ids SHALL be inserted unchanged; partial model matching may use literal prefix or case-insensitive substring but never fuzzy model correction/guessing. Completing a model SHALL not make it selectable or launchable; command dispatch must recheck the separate picker/runtime contracts.

#### Scenario: Snapshot proof expires between commands
- **WHEN** current time reaches a cached VERIFIED row's fresh_until
- **THEN** completion SHALL label it STALE snapshot / recheck required without network or renewal

#### Scenario: Unverified local model matches a partial argument
- **WHEN** an exact UNVERIFIED cached id matches `/probe cc/`
- **THEN** it MAY appear as a clearly labelled unverified suggestion and SHALL not be described as working or promoted through completion

### Requirement: Completion results and interaction SHALL be bounded and never auto-execute

Completion SHALL default to at most 20 results and hard cap at 50, over a pre-indexed bounded local catalog; overlong tokens above 256 characters SHALL produce bounded help or no candidates. Ordering SHALL be deterministic. Offline 6.7k/7,000-model fixtures SHALL enforce result bounds and test a target under 100 ms per completion on supported CI hardware. complete_while_typing SHALL remain false. Tab SHALL open/cycle the menu. Enter with an active choice SHALL insert it and close the menu without command submission; only a later explicit Enter SHALL submit the line through normal consent/dispatch. Esc SHALL dismiss the menu without applying/executing it. Completion SHALL never auto-execute a probe, refresh, bootstrap or launch.

#### Scenario: Large model catalog stays bounded
- **WHEN** an empty model argument matches 6,773 frozen models
- **THEN** completion SHALL yield at most the configured result cap in deterministic order with no full-catalog UI table or live work

#### Scenario: Tab Enter Esc do not dispatch
- **WHEN** the owner cycles with Tab, accepts a choice with Enter, or dismisses it with Esc
- **THEN** those completion actions SHALL not run an action; acceptance SHALL only modify the input and require a separate submission

### Requirement: Secret and credential argument completion SHALL be forbidden

Completion SHALL not inspect credential stores, auth.json values, tokens, environment values, arbitrary filesystem paths or secret history. Arguments marked secret, including credentials.set name/value fields, SHALL produce no argument candidates. Existing credential command names MAY remain in command help, but their presence SHALL not expose argument values. Candidate descriptions and source errors SHALL sanitize untrusted catalog/command text.

#### Scenario: Credential field cannot leak local values
- **WHEN** completion is requested after `/credentials` for secret fields or a credentials.set field prompt
- **THEN** no credential/environment/history-derived argument candidates SHALL be emitted and no secret source SHALL be read

### Requirement: The fallback loop SHALL provide equivalent syntax and typo help

The plain input loop SHALL use the same command grammar, syntax_help and suggest_command engine as prompt_toolkit. `/help [command]` SHALL display matching descriptions, fields-versus-flags, bootstrap/refresh consent, snapshot freshness and mode limitations. Unknown slash typos SHALL show bounded hints without execution. The fallback MAY show bounded local argument examples from the frozen snapshot but SHALL not claim terminal Tab menu support. It SHALL preserve existing quit/clear/Ctrl-C/EOF behavior and palette action/launch regressions, without starting background refresh for help or hints.

#### Scenario: Fallback typo is safe and equivalent
- **WHEN** the plain loop receives `/elegibility`
- **THEN** it SHALL print the same `/eligibility` hint and help wording, with zero action/refresh/probe calls

#### Scenario: Fallback syntax clarifies bootstrap limitations
- **WHEN** `/help bootstrap` is entered in either prompt path
- **THEN** both SHALL describe Prime preview/confirm/restore, read-only Claude modes and interactive-only scope, while plain input SHALL state its lack of a Tab menu
