# Local command completion and help

The Verdict prompt uses the existing prompt_toolkit framework with a frozen local
snapshot. Completion never fetches inventory, starts or joins refresh, waits for
proof, probes, runs credential helpers, or admits/launches a model. There is no I/O
on keystrokes. It is a convenience tool, not evidence or execution authority.

## Commands and syntax

Use `/help` for the palette or `/help bootstrap`, `/help eligibility`, and
`/help probe` for syntax and limitations. Bare command names also work.
The same immutable grammar drives completion, descriptions and syntax help.

```text
/eligibility [status] [provider=..] [search=..] [page=..] [refresh]
/probe <ids>
/bootstrap prime [ids...]
/bootstrap prime restore [transaction=<id>]
/bootstrap claude [ids...] [mode=native|openai-side-path]
/trace <run_id>
```

Exact model lists accept spaces or commas. TUI fields such as `provider=cc` are
not CLI `--flags`. Pasted `/probe --probe`, `/bootstrap prime --probe` or
`/eligibility --probe` refuses before consent or model calls. `--help` shows help;
it never becomes a model id. Other invalid fields show help without repair.

Command/choice matching ranks prefix, substring, then bounded fuzzy/typo matches,
with stable lexical ties. `/elegibility` offers `/eligibility`; it does not execute
it. Model identities use exact local ids and literal prefix/substring matching,
never fuzzy model guessing, suffix aliases or case-changing insertion. Slash run
commands also complete already-loaded local run ids. Targets are `prime`/`claude`;
Claude report modes are `native`/`openai-side-path`. There is no Claude apply.
Marked secret fields and credential values have no argument completion or history
lookups. Descriptions and fields/flags/choices help use the same grammar.

## Keys: insert is not execute

- **Tab** opens the bounded menu. Further Tab presses cycle suggestions.
- **Enter with a menu selection** inserts the selected candidate and closes the
  menu. It does not submit a command. A second Enter submits the typed line to
  normal dispatch and all consent gates.
- **Esc in the menu** closes it and restores the pre-menu line without executing.
- Outside completion, **Esc/Ctrl-C** during picker refresh stops future dispatch
  through the existing shared cancellation rules. Default-No consent never treats
  cancellation/EOF as Yes. Ctrl-C at the command prompt returns to the prompt;
  EOF exits. Nothing auto-executes, even when one suggestion matches.

`complete_while_typing=False` remains. Results default to 20, hard-capped at 50.
Tokens are bounded at 256 characters, input at 4096 characters, model rows at
10,000 and already-loaded run rows at 200. Large local catalogs are indexed outside
completion; no large Rich table or background wait is built on Tab.

## Snapshot lifecycle and health labels

After an explicit verified-view or Prime picker consumer finishes refresh and
reloads evidence, a separate post-render publisher writes
`~/.verdict/verified-completion-snapshot.json` (`VERDICT_HOME` can relocate it).
The read action remains read-only. Publication reads one bounded local evidence
generation and materializes full projection pages from those same inputs; the
rendered 50-row page is not mistaken for the full local catalog.

Only generation/time, source warnings, provider names and safe identity/status/
proof timestamp/coding metadata are stored. No credentials, raw provider errors,
URLs, account bindings or settings enter the file. A unique atomic replacement
writes it at 0600. Maximum size is 4 MiB; excess rows/bytes truncate deterministically
with an incomplete warning. Source errors, including an incomplete snapshot,
suppress **all model suggestions**, rather than silently offering partial proof.
Publication failure warns but does not invalidate health, runtime admission or
apply. The previous valid snapshot may still be used.

The prompt loads the file once on entry and only reloads it after an explicit
command. Missing, corrupt, unsafe-mode, symlink, future-dated, unsupported-schema
or oversized files produce no model suggestions and help says
`snapshot unavailable; run /eligibility explicitly`. There is no live fallback.
Deleting the cache leaves command/static help completion working.

`VERIFIED snapshot` means historical local proof, **not launch authority**. At
`now >= fresh_until`, the same row displays `STALE snapshot; recheck required`.
Missing timestamps are not called working. Other statuses stay visibly unverified
or blocked. Display age uses an injected prompt time, not filesystem/network work.
Completing a candidate does not make it selectable: the command must separately
refresh/reload and recheck health, visibility, transport, config and admission.

## Plain input fallback

When prompt_toolkit cannot start, plain `input()` retains identical syntax help
and nonexecuting typo hints. Use `/help bootstrap` and `/help eligibility` in
that loop too. Plain input cannot offer a terminal Tab menu; Verdict does not
invent one or claim Tab support there. No background refresh runs in either loop.

See [harness bootstrap](harness-bootstrap.md) for the preview/consent/restore
workflow, Alt+M-only scope, separate visibility sync and Claude BOD-102 limits.
