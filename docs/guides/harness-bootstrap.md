# Harness bootstrap: exact Prime scope and honest Claude reports

Verdict separates health, registry visibility, transport compatibility, interactive
scope, and runtime admission. A catalog entry proves none of the other facts.
This workflow makes no claim that a selected model was tested end-to-end in Prime
or Claude Code. Existing harness enable/disable/certify commands remain unchanged.

## Prime workflow

1. Run `/eligibility` in the Verdict prompt, or
   `verdict eligibility --verified --json`. The verified view uses bounded refresh,
   waits with progress, then reloads evidence. Legacy `verdict eligibility --json`
   still uses its original eligibility envelope.
2. Run `/bootstrap prime` for a bounded current-page picker. Enter exact ids,
   separated by spaces or commas. Or enter `/bootstrap prime cc/sonnet cc/opus`.
   The CLI equivalent is `verdict harness prime select cc/sonnet cc/opus`.
3. Read the preview. It shows the resolved global settings path, exact before/after
   scope, additions/removals, retained entries, health, visibility, compatibility,
   warnings, digests and the original proof deadline.
4. Answer the apply prompt `[y/N]`. Blank, No, EOF and cancellation never apply.
   After Yes, Verdict refreshes only the involved ids again and reloads proof.
   It checks settings, dependencies, scope and freshness under a private lock.
   Changed facts or an expired original deadline refuse; rerun for a new preview
   and a new decision. No silent repair or narrowed apply uses old consent.

Automatic picker refresh covers eligible prepaid routes only and can consume
prepaid quota. It respects coordinator route/request/time/concurrency caps and
`VERDICT_AUTO_REFRESH`. It does not sweep the catalog. Metered/unknown routes get
a separate exact refresh plan, with ids, estimated requests, caps and a quota
warning, then their own default-No spend decision. Currency estimates are not
available. Confirmed metered/unknown refresh is chat-only liveness, not tools or
coding proof. It cannot upgrade identity by consent. Esc/Ctrl-C during refresh
stops future dispatch under the shared coordinator cancellation rules. A current
request may finish. Cache evidence may persist, but cancel does not change scope.

`verdict harness prime select <ids> --preview --json` stops before settings
mutation. JSON output is one final envelope on stdout; progress, preview and
prompts go to stderr. Without a real interactive stdin, select/restore return a
preview only. Piping `y` is not apply consent. A separately authorized caller can
use the digest-bound preview/apply action API with literal `confirmed=True`.

Only current VERIFIED rows with verified identity, strict future freshness, exact
Prime visibility and compatible transport/readiness can be added. Coding/tools
and agentic proof stay separate: chat-only and nonfresh agentic facts are visible
warnings, not a hidden coding floor. Unknown restrictions, cooldowns, failed,
stale, unsupported and unverified rows block selection. Compatibility means
`compatible_not_launch_confirmed`, never a launch permission.

## What Prime changes, and what it does not

The reader resolves `PRIME_AGENT_CODING_AGENT_DIR`, otherwise `~/.prime/agent`, and
reads the real installed binary path without running it. The supported exact-id
resolver is release 0.9.8. Unknown releases, unsafe/missing documents, symlinks,
unknown credential readiness, ambiguous names/effort suffixes or unsafe effective
paths refuse. Credential helper commands are never executed by discovery.

The transaction changes only global `settings.json` `/enabledModels`. Raw exact
gateway ids are used, not human names or guessed suffixes. An explicit leading
`omniroute/` is canonicalized; case, literal colons and slashes remain exact.
There are no wildcard, alias, `auto/*`, default or fallback selection repairs.

The scope replaces uniquely bound `omniroute` entries. Other-provider exact entries
stay in their original order. Unknown historical exact entries are retained and
labelled not verified: this is not a full verified allowlist. Existing wildcard
or ambiguous scope refuses; resolve it through Prime `/scoped-models` first.
The default model, registry metadata, credentials and project settings are not
changed. A default outside the scope is warned about, not repaired.

`enabledModels` controls Alt+M interactive cycling only. New sessions read it;
existing sessions may need `/scoped-models` or a restart. Project settings and
explicit `--models` can override it. Verdict still chooses an exact worker route
and reconfirms runtime admission immediately before dispatch. This selection does
not authorize any worker, constrain all future launches, or change certification.

## Separate visibility sync

If a healthy id is **not visible to Prime**, inspect the separate inventory sync:

```sh
verdict harness prime sync-models --dry-run --json
verdict harness prime sync-models
```

The write asks for explicit confirmation. This operation updates inventory
visibility, not health or launch proof. The picker never syncs implicitly.
After a separate sync, rediscover and rerun the picker; discard the old preview.

## Safe restore and concurrency limits

Use `/bootstrap prime restore`, optionally `transaction=<id>`, or:

```sh
verdict harness prime restore --preview --json
verdict harness prime restore --transaction <id>
```

Restore previews the reversal and asks `[y/N]`. It restores prior scope, **not
newly verified models**. It refuses after ANY later settings edit, even whitespace
or an unrelated key. There is no force mode. Use the selection receipt, not old
`harness prime disable`, which restores a different models.json enable backup.

Settings transactions use a nonblocking cross-process advisory lock, exact byte
digests, unique atomic replacement, immediate private backups, and private
prepared/applied receipts. Settings and bookkeeping files are 0600. Five completed
owned pairs are retained; incomplete recovery records and unrelated files stay.
Backup bytes can contain secrets. Do not print or share them. Parent directories
must be owner-controlled and not group/world writable; unsafe directories refuse.

The lock coordinates Verdict writers. It cannot stop arbitrary external editors.
Repeated checks detect observed changes, but ordinary files have a small
check-to-replace race with noncooperating writers. Avoid concurrent settings edits.
An incomplete receipt after durable replacement is reported honestly with recovery
instructions, not an automatic overwrite or false "nothing changed" claim.

## Claude Code: report only

```text
/bootstrap claude cc/claude-opus-5-5
/bootstrap claude cc/claude-opus-5-5 mode=openai-side-path
```

```sh
verdict harness claude compat cc/claude-opus-5-5 --mode native --json
verdict harness claude compat --mode openai-side-path --json
```

Native Anthropic Messages (`/v1/messages`) is **unsupported / NEEDS_OWNER BOD-102**
for every exact id, even a freshly VERIFIED Claude-looking route. There is no
native proxy in this feature. The existing env/SessionStart gate is reported as
**OpenAI side path; not native Claude Code selection**. Configured side-path facts
are unproven for exact selection: `selected_id_unproven`,
`exact_selection_tested=false`, `selectable=false`, `apply_available=false`.

The reader performs no refresh, probe, certification, helper execution, settings,
hook, backup or receipt writes. It compares settings byte digests before report
presentation. A change blocks old side-path readiness. Missing binary/settings or
credential presence stays explicit. Omitted ids use current exact Prime scope
joined to the last local completion projection, labelled with that source. Missing
local input yields an empty diagnostic report, not a live catalog sweep.

There is no Claude apply/select/force/restore branch or selected-models write API.
Native support needs BOD-102 and separately designed exact end-to-end evidence.
See [local completion](tui-completion.md) for snapshot limits and prompt help.
