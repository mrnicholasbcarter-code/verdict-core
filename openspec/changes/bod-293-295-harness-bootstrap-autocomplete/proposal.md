# Proposal: Harness bootstrap and context-aware autocomplete

## Why

BOD-293 needs a safe way to put exact, evidenced model ids into Prime's interactive picker without treating a catalog as working models. BOD-294 must expose the Claude Code native-transport block honestly, and BOD-295 must make the existing command prompt easier to use without live work on each keystroke.

## What Changes

- Add a guided Prime picker with independent health, visibility, and exact-id transport checks. Target only global `settings.json` `enabledModels`, with exact ids and a scoped replacement policy. Do not change `defaultModel`, provider registries, credentials, or worker admission policy.
- Reuse BOD-291's verified projection and BOD-292's bounded refresh. Picker preview and apply refresh only involved ids, wait with progress, reload evidence, and fail closed on missing proof. Automatic refresh covers eligible prepaid routes only; metered/unknown refresh requires a separate `[y/N]` spend decision.
- Add pure selection/restore previews and digest-bound, confirmed settings transactions. Use immediate private backups, a cross-process lock, minimal atomic writes, idempotency, and restore refusal after later edits.
- Add a read-only per-id Claude compatibility report. Native Anthropic Messages stays `unsupported / NEEDS_OWNER` pending BOD-102. Label the configured OpenAI side path separately and do not claim tested exact selection or add any Claude apply operation.
- Extend the current `prompt_toolkit` completer with bounded, deterministic command and argument suggestions, descriptions, fuzzy/typo hints, field/flag help, and explicit Tab/Enter/Esc behavior. Use frozen local snapshots only. The plain input loop gets the same syntax and typo help, not an invented terminal-completion engine.
- Add `/bootstrap prime [ids...]`, `/bootstrap prime restore`, and `/bootstrap claude` with optional exact-id/mode report filters. Preserve `/eligibility [status] [provider=..] [search=..] [page=..] [refresh]` and consented `/probe <ids>`. Add non-colliding CLI subcommands `verdict harness prime select|restore` and `verdict harness claude compat`.

## Capabilities

### New Capabilities

- `prime-verified-selection`: health/visibility separation, exact interactive scope, refresh/consent sequencing, and guarded selection/restore transactions.
- `claude-model-compatibility`: read-only discovery and per-id native/side-path compatibility without unsupported selection writes.
- `local-command-completion`: local-only command/argument completion and equivalent fallback help, with bounded results and no side effects.

### Modified Capabilities

None. This change consumes the sibling `verified-model-projection` and `bounded-model-refresh` contracts without redefining them. Existing harness enable/disable/certify and legacy eligibility remain unchanged.

## Impact

- U4 owns new Prime transaction/compatibility modules and tests. U5 owns new Claude report modules and tests. U6 owns one new pure completer module and tests. U7 alone owns the bootstrap adapters, snapshot publisher, command/registry/CLI wiring, and user documentation.
- U7 starts shared-file integration only after BOD-291 unit 2 and the unit-3 fix commit for confirmed METERED/UNKNOWN execution land. Actual argparse registration and dispatch live in `verdict/commands/parsers_harness.py` and `verdict/commands/dispatch.py`, so these also belong to U7, not a parallel worker.
- No new framework, dependency, Anthropic proxy, model-name guessing, `auto/*`, default/fallback selection, catalog sweep, credential management, or changed certification claims.
- This change is design only. Implementation will write only a confirmed Prime settings transaction and its private bookkeeping, or a sanitized local completion snapshot outside keystroke callbacks. This design task makes no live calls and changes only this OpenSpec directory.
