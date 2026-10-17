# Hermes memory plugin (BOD80 lane C)

Status: **experimental, PARTIAL**. This page describes what the code on branch
`bod80/lane-c-hermes` actually does. Hook behavior is taken from the installed
Hermes source and from Hermes's own plugin loader in a temporary `HERMES_HOME`. A
live Hermes turn has **not** been observed yet.

## What it is

`verdict.harness_hermes_memory` installs a small Hermes plugin named
`verdict-memory`. That plugin registers four native Hermes hooks. Each hook passes
a bounded JSON payload to:

```
python -m verdict.harness_hermes_memory hook <hook-name>
```

`verdict harness hermes enable` now installs this plugin after switching the
provider, and `verdict harness hermes disable` uninstalls it before restoring the
config backup. If `plugins/verdict-memory` exists and is not Verdict-owned,
`enable` (even with `--force`) fails with `hermes_memory_plugin_install_failed`
before it writes `config.yaml`, so nothing changes. `disable` always restores
`config.yaml`; if the plugin folder cannot be removed it then fails with
`hermes_memory_plugin_uninstall_failed` and leaves that folder for you to remove.

Verdict then turns the payload into a `lifecycle-event/v1` event and handles
recall or capture. The plugin itself contains no capture policy. It uses only the
standard library and catches every error, so a memory failure never breaks a
Hermes turn.

The plugin does **not** use Hermes's `memory.provider` slot, which allows only one
provider at a time. Hermes's built-in memory keeps working.

## Install and uninstall

```python
from pathlib import Path
import verdict.harness_hermes_memory as m

m.install(hermes_home=Path("/path/to/hermes-home"))    # defaults to $HERMES_HOME, then ~/.hermes
m.uninstall(hermes_home=Path("/path/to/hermes-home"))
```

`install`:
- copies `plugin.yaml` and `__init__.py` from `verdict/hermes_plugin/verdict_memory/`
  into `<HERMES_HOME>/plugins/verdict-memory/`;
- writes `config.json`, which holds the Verdict command and a hook timeout (8 s
  by default);
- writes the ownership marker `.verdict-owned.json`;
- adds `verdict-memory` to the end of `plugins.enabled` in `config.yaml` and leaves
  every other entry (for example `orca-status`) and every other key alone;
- the first time it edits the config, backs it up once to
  `config.yaml.verdict-memory.bak`;
- does nothing on a second run: the files stay byte-identical and no second backup
  is made;
- raises `HermesMemoryCollisionError` and changes nothing if
  `plugins/verdict-memory/` already exists without a Verdict marker.

`uninstall`:
- removes only `verdict-memory` from `plugins.enabled`, and removes the `plugins:`
  key only if the install created it and it is now empty;
- deletes only the plugin directory that carries the Verdict marker;
- removes `<HERMES_HOME>/plugins/` as well, but only if the install created it
  and it is empty once the plugin is gone; other plugins and user files are kept;
- refuses on a user-owned collision, just like install.

Known limit: config edits go through PyYAML, as in the existing
`verdict harness hermes enable`, so comments in `config.yaml` are not kept when
the file is rewritten. The parsed content is kept.

## Hooks used and event mapping

| Hermes hook | Fires at (v0.21.3 source) | Canonical kind | Verdict action |
|---|---|---|---|
| `on_session_start` | `agent/conversation_loop.py:774` | `session_start` | translated, no action yet |
| `pre_llm_call` | `agent/turn_context.py:697` | `before_first_model_turn` | bounded recall; returns `{"context": ...}` |
| `post_llm_call` | `agent/turn_finalizer.py:434` | `before_yield` | capture of the assistant response |
| `on_session_end` | `agent/turn_finalizer.py:663`, `cli.py:873` | `session_end` | translated, no action yet |

Loader references: `hermes_cli/plugins.py:1-9` (directory plugins), `:108`
(`VALID_HOOKS`), `:916` (`register_hook`), `:1424` (`plugins.enabled` gate). Hermes
adds whatever `pre_llm_call` returns to the current user message
(`website/docs/developer-guide/observer-hooks.md:62`).

Event identity:
- `source_harness=hermes`;
- `project` and `workspace` come from Verdict configuration (the runtime uses
  `VERDICT_MEMORY_PROJECT`, falling back to the name of the working directory);
  a `project` or `workspace` field inside a hook payload is ignored;
- `source_event_id = hermes:<session_id>:<turn_id>:<hook>`, so replaying the same
  turn produces a single logical event;
- `content` is passed through `verdict.shared_memory.redact_secrets` and capped at
  8,000 characters, and provenance fields (`model`, `platform`, `sender_id`,
  `cwd`) are redacted too;
- canonical JSON and the logical digest follow P's rules: sorted keys, compact
  separators, UTF-8, and `observed_at`/`deadline_at` excluded from the digest.

Recall output is bounded (2,000 characters by default). It is wrapped in a header
that marks it as untrusted recalled evidence, not instructions, and each line
cites its `source_event_id`.

## Capability matrix (Hermes Agent v0.21.3, upstream 2ed6387d, read 2026-10-16)

| Capability | Status | Evidence |
|---|---|---|
| Automatic recall before each turn | PARTIAL (source-evidenced) | `pre_llm_call` context injection; not seen live |
| High-value capture | PARTIAL (source-evidenced) | `post_llm_call`; not seen live; no backend yet |
| Session identity | PARTIAL (source-evidenced) | `session_id` and `turn_id` in hook kwargs |
| Project identity | PARTIAL | not provided by Hermes; comes from Verdict config |
| Source event id | PARTIAL | built from `session_id`, `turn_id` and the hook name |
| Reversible install | SUPPORTED (tested in a temp `HERMES_HOME`) | `tests/test_harness_hermes_memory.py` |
| Tool pre/post, verification, compaction, subagents | UNSUPPORTED by this adapter | the hooks exist in Hermes but are not wired |
| Resume | UNKNOWN | not traced |

`verdict.harness_hermes.certify` now reports `hooks`, `session_start`,
`before_first_turn`, `session_end` and `mutation_capability` as `partial`. It no
longer says Hermes has no hook system.

## Backend failure, outbox and gaps

- **Recall failure:** the hook returns no context with gap
  `memory_backend_unavailable`, and the failure is reported to an admission
  interface so Verdict can refuse to dispatch new work. The real gate belongs to
  E; tests use a fake.
- **Capture failure:** the event goes to a bounded JSONL outbox (500 events by
  default; the oldest are dropped and counted), and the same gap is reported.
- **Healthy but empty recall** returns status `empty`, which is distinct from an
  error.
- **No backend configured.** This is the current runtime state, because the P
  client is on security hold. Recall reports gap `memory_backend_not_configured`,
  and captures go to `<HERMES_HOME>/plugin-data/verdict-memory/outbox.jsonl`.

## Backend seam

`_LifecycleBackendAdapter(provider, load_event=..., make_query=...)` connects the
bridge to a provider with P's `put(event)` / `search(RecallQuery)` interface:
- each event is passed to `load_event` as P canonical `lifecycle-event/v1` bytes;
  with P, that function is `verdict_memory.loads_lifecycle_event`;
- recall builds its query with `make_query` (P's `RecallQuery`), scoped to the
  configured project and workspace;
- any search status other than `available`, or a raised error, becomes gap
  `memory_backend_unavailable`, never a healthy empty result;
- a failed `put` queues the event in the bounded outbox.

The plugin stamps `hook_fired_at` (Hermes-side time when the hook fired), and that
becomes the event's `created_at`. `provenance.created_at_source` records whether
the time came from `hermes_hook_fire` or, as a fallback, `adapter_receipt`.

Verdict does not import the package itself, because E owns that dependency. A
one-off run against P's real `FakeMemoryProvider` (P worktree on `PYTHONPATH`, no
install) captured an event, deduplicated a replay, recalled it with a citation and
no leaked secret, and reported the gap when the provider status was `UNAVAILABLE`.

## Live observation (2026-10-16 19:21 ET)

A single bounded `hermes chat -Q --max-turns 1` turn, run with Hermes Agent
v0.21.3 in a temporary `HERMES_HOME`. Setup:
- model `agy/gemini-3.1-flash-lite` via the OpenAI-compatible endpoint
  `127.0.0.1:20128/v1`;
- the key was referenced by environment variable name only;
- branch head `5228c38e`;
- the staged smoke script lives outside the repo.

Observed:
- The plugin loader parsed and loaded `verdict-memory` (source=user) and
  registered its hooks.
- All four hooks fired in order: `on_session_start`, then `pre_llm_call` (gap
  `memory_backend_not_configured`), then `post_llm_call` (queued), then
  `on_session_end`.
- One outbox row was written: kind `before_yield`, `source_event_id` beginning
  `hermes:<session_id>:`, `created_at_source=hermes_hook_fire`.
- Hermes exited 0 and the model replied `ok`.
- Canary and key hits in the outbox, stdout, stderr, hook log and evidence file:
  all 0.
- Uninstall succeeded and the temporary directory was removed.

Observed turn ids are composite (`<session>:<uuid>:<hash>`), so `source_event_id`
is long but still unique per turn.

Facet status after this run: `session_start`, `before_first_turn`, `session_end`
and capture (`mutation_capability`) have been seen **firing** live. `certify` still
reports them as `partial`, because no live backend round trip exists yet (P's
client is on hold) and recall injection with real content has not been observed.
Any change to SUPPORTED is the controller's decision.

## Live redaction check (2026-10-16 19:31 ET)

Same setup as above, with the model asked to echo two fake keys. Its reply was
`key1 sk-lanecfake<hex> api_key=sk-lanecfake<hex>`. The captured outbox row stored
`key1 sk-lanecfake<hex> api_key=[REDACTED]`:
- the `api_key=<value>` form is redacted;
- a **bare** `sk-...` token is **not** redacted and reaches the outbox raw, because
  `redact_secrets` only matches `bearer`/`basic` values, `token|secret|password|api_key`
  assignments and URL user-info;
- the real key appeared 0 times in all files.

BOD-423 adds a fail-closed bare `sk-` guard to the `verdict-memory` client, so once
this adapter is wired to `AiMemoryClient` such tokens are redacted before they leave
the host (see `packages/verdict-memory/docs/TRANSPORT.md`). The local JSONL outbox
above still goes through `verdict.shared_memory.redact_secrets`, which this lane does
not own, so a bare token can still be stored there until that function is widened.

## Not proven yet

- Redaction of bare credential tokens (e.g. `sk-...`) in the local Hermes outbox; see above.
- Recall injecting real content into a live turn (only the no-backend gap has
  been observed).
- A real backend round trip (only P's Fake has been exercised), since P's `AiMemoryClient` is on security hold, and
  the Hermes→Codex and Codex→Hermes pairs.
- Draining the outbox to the backend, which waits on E/P wiring.
- `on_session_start` and `on_session_end` are translated but not acted on.
