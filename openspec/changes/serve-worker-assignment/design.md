# Design

Facts that come from code merged in pull request #1299 (the OpenCode worker adapter) are marked
**[#1299]**. Choices that are mine, not one of the operator's eight decisions of 2026-11-24 on
#1313, are labelled **design choice**; they are listed again under "Proposed additions" when they
go beyond what the decisions state.

## Context

`verdict serve` has one relay path (`POST /v1/chat/completions`, `/v1/responses`, `/v1/messages`;
all three call `_relay_completion`). A relay request is routed by `IntelligenceService.route`
(`_route`), then forwarded by the relay with bounded attempts, each attempt dispatched on a
route-bound admission proof that admission minted and the relay re-verifies.

How a worker is pinned today **[#1299]**: the relay treats any request carrying an
`X-Verdict-Worker` header as a worker, wraps its `model` field in `WorkerAssignedRoute`, and
`_decision_from_admit` selects that route only when it is in the admitted set, after the whole
ladder has run. The credential is the serve bearer token (`resolve_serve_token`, swapped in at
`_worker_credential`). Anyone holding the serve token can send the header and name any admitted
route, and the whole ladder, keyword scanner included, runs on the request text first.

`_route` order today (read at `51c935ae`):

1. refuse a worker with `execution_path_request` (**[#1299]**);
2. resolve `execution_path_request`, and if an `ExecutionPathDecision` exists, return it;
3. `if require_authority: raise ExecutionPathError(...)` (production profile, or
   `VERDICT_REQUIRE_EXECUTION_PATH`);
4. legacy path: `scan(task)` (keyword floor), planner effort and tier, then
   `_offload_free_tier` and `_decision_from_admit`.

`_offload_free_tier` does, in order: load the OmniRoute snapshot (fail closed on the relay path if
unavailable); `classify_worthiness` (text); `planner.plan` capabilities (text: the words
"implement", "build", "code", "api" or "test" add `tool-calling`); `derive_requirements`;
`profile_task` (digest); `admit_free_tier_active`; cooling; `expand_admit_for_worthiness`;
`gate_capability` (metadata withheld on the relay path); `_apply_candidate_pool` (only when
metadata was passed); `gate_admit_prove_confirm`; `_estimate_candidate_context_plans`; the
`EligibilityGate`; `apply_best_of_admitted`; `_decision_from_admit` (builds the cheap-path pack,
mints the proof with `_chosen_route_proof`).

The relay then recomputes the task profile with `prepared_task_profile(task, authority_context,
planner=...)` and denies a proof whose scope does not match ("proof scope or observation
mismatch"); it injects the compiled pack into the forwarded body when the decision carries the
`cheap_path_context_pack` flag.

## Goals / Non-Goals

**Goals**

- Serve routes a request on the route the orchestrator assigned, on proof of that assignment, and
  on nothing else the request says.
- Every per-route hard gate still runs on that route. Nothing is re-admitted.
- The four blockers (scanner, missing metadata, pack size and injection) and the two ladder
  interactions (streaming evidence, Top-K starvation) stop refusing a worker whose route passes its
  gates.
- A forged, expired, tampered, mis-keyed or absent assignment never reaches an upstream.

**Non-Goals**

- The executor signing the assignment and carrying run and node ids (#1325). This change defines
  and verifies the format; it does not add a signing function (see "Hand-offs").
- The dispatch-origin check (#1326), instructions-file delivery and the status-matcher fix
  (#1327; the matcher, `_STATUS_RE`, is **[#1299]** code), the default switch (#1328).
- Stream evidence (ADR-013 probes persisted as evidence). Streaming is waived instead (decision 5).
- A keyword floor on node text in the orchestrator. Parity with the `direct-gateway` path is the
  decision (6).
- Production-profile serve for workers (decision 7).
- Stopping a hostile shell inside a worker (ADR-053 decision 4, accepted risk).
- Any change to `verdict/admission.py`, `build_attempts`, the proof or permit code, or the
  relay's attempt, retry and streaming handling.

## Decisions

### D1. The assignment is the worker's bearer credential (design choice)

**Format.** One opaque string, the worker's `Authorization: Bearer` value:

```
vwa1.<payload>.<tag>
payload = base64url(JSON {"exp": <int>, "node_id": <str>, "route_id": <str>, "run_id": <str>})
tag     = base64url(HMAC-SHA256(key, "verdict-worker-assignment/v1\n" + payload))
```

`vwa1` is the format version. `exp` is integer seconds since the Unix epoch, UTC (an integer
cannot be a naive local time; a boolean, float, negative or non-integer is refused), and it has an
upper bound: `0 <= exp <= 253402300799`, which is 9999-12-31T23:59:59Z, the last second a UTC
`datetime` holds. The bound is needed because the verified result carries `expires_at` as a UTC
time and every receipt writes it in ISO form; a larger integer, such as 253402300800 (a
`ValueError`, year 10000) or 2^63 and above (an `OverflowError`), carries a valid tag and still
cannot become that value. The domain
tag keeps a tag made for this purpose from being valid for any other HMAC use of the key.
`route_id`, `run_id` and `node_id` are non-empty strings of at most 128 characters with no control
characters (they are written to logs and receipts). Unknown or missing fields are refused. A
token over 2 KiB is refused before decoding.

**Verification** (`verdict/worker_assignment.py`, new; pure, no I/O except loading the key):
split on `.` into exactly three parts; decode the tag; recompute the HMAC over the received payload
text; compare with `hmac.compare_digest`; only after the compare succeeds decode and validate the
payload (shape, field limits and the `exp` range above); convert `exp` to the UTC `expires_at`
inside a guard that maps `OverflowError`, `ValueError` and `OSError` to `malformed`, so no value
that is not a timestamp passes the verifier and none raises out of it; then check `exp > now`
(strict, UTC, injected clock). A correctly signed `exp` of 0 is well-formed and ends `expired`; one
above the bound ends `malformed`, not `expired`. Every refusal is one reason code
(`malformed`, `bad_signature`, `expired`, `key_unavailable`) that is logged by serve and never
echoed to the caller: the caller gets the same generic body as for any wrong bearer, so an
unrepresentable `exp` is a 403 and never a 500.

**Result type.** A frozen `VerifiedWorkerAssignment` with `route_id`, `run_id`, `node_id`,
`expires_at` (a UTC time with an explicit offset, `Z`, in every receipt) and `assignment_digest`
(SHA-256 of the payload text, never of the token). It is constructible only by the verifier (a module-private construction token), travels only as a
server-set value (`request.state`, then a context key that JSON cannot supply), and replaces
`WorkerAssignedRoute` **[#1299]**.

**Replay.** An assignment is a bearer credential valid until `exp` for one route: a worker makes
many requests per attempt (13 in the smoke), so it cannot be single-use. Expiry is checked on every
request, so an attempt that outlives its assignment fails closed on its next request. #1325 sets
`exp` from the attempt's timeout (the 900 s ceiling in `orchestration/cli.py` and `runtime.py`)
plus margin.

**Why the bearer, not a second header.** The research (fact 9) requires the worker to hold no
serve token: with the serve token in its environment the worker could call `/v1/route`,
`/v1/runs`, receipts and guidance, and the assignment would narrow nothing. With the assignment as
the credential, the worker holds nothing that serve accepts anywhere except the relay and nothing
that names a route other than its own. `_check_serve` **[#1299]** (a read-only
`GET /v1/guidance/status`) then needs the real token in the orchestrator process, not in the
child; that belongs to #1325.

### D2. Where an assignment is accepted (design choice)

In `caller_authentication` (existing middleware), after the serve-token compare fails (the
anonymous branch has a rule of its own, after the table):

| Request | Result |
|---|---|
| bearer equals the serve token | unchanged |
| bearer has the `vwa1.` prefix, `POST /v1/chat/completions` or `POST /v1/responses`, key loads, token verifies | `request.state.worker_assignment` is set; request continues |
| same, key file missing, unreadable or invalid | 503 `worker assignments are not configured`; fails closed |
| same, token fails verification | 403 `authentication failed` (the existing wrong-bearer body) |
| bearer has the `vwa1.` prefix on any other path or method, including `/v1/messages` | 403 `authentication failed` |
| an assignment in `x-api-key` on `/v1/messages` | 403 (that header only carries the serve token) |
| anonymous mode (`LLMGATE_ALLOW_ANONYMOUS` and no token), loopback client, request carries an assignment (bearer or `x-api-key` with the `vwa1.` prefix), any path or method | 403 `authentication failed`; nothing verified, the key never loaded |
| anonymous mode, loopback client, no assignment | passes as today; a non-loopback client keeps its 403 `anonymous mode is loopback-only` |

The two OpenAI surfaces are the ones the worker uses (**[#1299]** the per-attempt config is an
OpenAI-compatible provider); the Anthropic surface and the `x-api-key` path are left out to keep
the accepted surface to what a worker needs.

**The anonymous branch needs its own check.** Today `caller_authentication` returns
`call_next` for every path as soon as the loopback test passes in anonymous mode, before it reads
any header, so the table rows above that depend on the serve-token compare never run there. Without
a rule of its own, a loopback anonymous request with an assignment bearer to
`GET /v1/guidance/status` or `POST /v1/route` would be passed on. So the middleware gets one
path-independent check, placed in the anonymous branch after the loopback test and before
`call_next`: if the `Authorization` bearer value or the `x-api-key` value has the `vwa1.` prefix,
answer 403 `authentication failed`. That check only looks at the prefix. It never loads the key and
never verifies: anonymous mode has no serve token to stand behind and grants no assigned lane, so
refusing the credential outright is the fail-closed answer. A request without an assignment is
passed on exactly as today.

**Two paths never reach the check.** `/health` and the OmniRoute webhook path return before any
credential is read, with or without the serve token, so an assignment presented there is ignored:
it sets nothing on `request.state`, authorises nothing, and neither handler reads it. The spec names
these two exceptions instead of claiming a rule it cannot keep.

In the relay, a request is **worker-marked** when it carries the `X-Verdict-Worker` header or a
bearer with the `vwa1.` prefix. A worker-marked request without `request.state.worker_assignment`
is denied 403 before any routing: no ladder, no proof, no upstream. This covers decision 4 (the
bare header, with the serve token or with nothing, selects nothing), the anonymous-mode header
case (the middleware check above catches an assignment bearer there, so the relay guard is the
only guard for the header alone) and the forged-prefix case. For an assignment bearer the relay
guard is a second line behind the middleware, and a test calls it with the middleware out of the
way. The header is neither required nor sufficient for a verified assignment; the assignment alone
assigns.

A worker-marked request that carries `execution_path_request` is refused 400 before routing,
exactly as **[#1299]** does, now keyed on the verified assignment; `_route` refuses the same
combination independently.

### D3. The `model` field must equal the signed route (decision 3)

Before routing, the relay compares the request's `model` to `assignment.route_id` by exact string
equality (no trimming, no case folding, no alias resolution). Missing, null, empty, non-string or
different is **denied**, never ignored and never rewritten: a pre-routing refusal, 400
`invalid_request_error`, with no ladder, no proof and no upstream call. Serve logs it with the
reason code `model_mismatch`, the signed route id, run id and node id, and never the token.

Why a pre-routing refusal and not the relay's denied-decision path: that path needs a routed
decision. `build_attempts` runs on the decision first, `_start_evidence` reads `attempts[0]`, and
`build_attempts` returns no attempt (503, no evidence) for a route the optional relay admission
cache (`VERDICT_RELAY_ADMISSION_CACHE`) does not list, which a synthetic `denied` decision would
hit. So this denial has a log line and no evidence record, and it saves the ladder's work for a
request that contradicts its own credential. (A refusal after routing, below, still goes through
the denied-decision path as today.)

After routing the relay verifies that the decision's route equals the signed route and clears any
alternatives, as **[#1299]** does, so an assigned request has exactly one attempt on exactly one
route. A decision for any other route is turned into a denied decision exactly as **[#1299]** does
today.

### D4. The signing key (decision 1)

A separate key, never the serve token.

- **Generated** by `verdict setup --apply --allow host.serve` (`serve_service.manage_service`),
  the same action that writes `serve.env`: `secrets.token_bytes(32)`, stored as URL-safe base64
  text. Idempotent: an existing valid key is kept, never regenerated. A dry run writes nothing.
- **Stored** in its own file, `~/.config/verdict/worker-assignment.key`, beside `serve.env`, mode
  0600 in a 0700 directory, written atomically (the existing `_write_private`). **Not** in
  `serve.env` (the unit's `EnvironmentFile` would put it in serve's process environment and
  `/proc/<pid>/environ`), **not** in the credentials store (`load_into_env` injects values into
  `os.environ`), and no code path sets it in any process environment.
- **Read** by serve on each assignment verification (assignment requests only; the serve-token
  path never touches it), and by the orchestrator when it signs (#1325), through the same loader.
  A loader result is never cached across requests, so a rotation takes effect on the next request
  with no restart. The loader decodes the text and requires at least 32 bytes; anything else is
  `key_unavailable`.
- **Missing or invalid**: serve answers 503 to an assignment request (D2) and signs and admits
  nothing; non-worker traffic is unaffected. `verdict doctor` reports the key file missing, shorter
  than 32 bytes or with a mode other than 0600: the serve row already comes from
  `serve_service.install_problems` through `setup_host`, which names `serve.env` problems and never
  a value, and the key file's problems join that list. Doctor and `install_problems` only read:
  they never generate, repair or rewrite the key file (only setup writes it).
- **Rotation**: remove the file and run `verdict setup --apply --allow host.serve`; serve uses the
  new key on its next assignment request. Assignments signed with the old key stop verifying at
  once, so an in-flight attempt fails closed and recovery reroutes it: rotate when no run is
  active. No dual-key window (a design choice that keeps the verifier to one key).
- **Never in a worker's environment**: the key is read only by serve and the orchestrator
  process. The executor's child environment is an allowlist **[#1299]** (`_CHILD_ENV_ALLOWLIST`);
  the worker receives only the signed assignment. **Be exact about the limit:** the allowlist
  includes `HOME`, and the worker's shell runs as the same Unix user, so a shell command can read
  the key file by path (and `serve.env`). That is the accepted unrestricted-shell risk of ADR-053
  decision 4. The assignment stops a *model* from choosing its route; it does not stop a hostile
  shell from minting one. A separate OS user or sandbox would, and is not proposed here.

### D5. The assigned lane (decisions 6, 7, 8; the research's recommended approach)

`_route` gains the lane **after** the `require_authority` raise (step 3 above) and before the
legacy path. Reachable only when the context holds a `VerifiedWorkerAssignment`. Its first act is
the explicit-protection check below. In production
profile `require_authority` is true, the raise fires first, and the relay answers 400
(`ExecutionPathError`): worker requests stay unsupported there and fail closed (decision 7).

The lane calls `_offload_free_tier` with the assignment as a parameter (one ladder, not a second
one) and, whenever that call cannot produce a decision (no live admission configured, `None`
returned), denies. It never falls through to the legacy catalog path, which would run the scanner.

What the lane skips, keeps and changes, step by step (default relay request versus assigned lane):

| Step | Default relay request | Assigned lane |
|---|---|---|
| `scan` keyword floor, planner effort and tier, advisory signals | runs | **skipped** |
| `classify_worthiness` (text) | runs | **skipped**; the receipt says it did not run |
| `planner.plan` capability inference (text) | runs | **skipped** |
| `derive_requirements` (request fields) | runs | runs, from request fields only |
| `profile_task` digest | runs | runs, through the shared helper (D6) |
| snapshot load; unavailable fails closed | runs | same |
| `admit_free_tier_active` (active, quota, opaque) | runs | same |
| cooling (negative health cache) | runs | same |
| narrowing to the pinned route | n/a | **right after cooling**; absent means denied, with the reason of the check that dropped it |
| `expand_admit_for_worthiness` | records class, applies an explicit `spend_policy`, stamps the digest | applies an explicit `spend_policy` (a filter that can only remove) and stamps the digest; no class computed |
| `gate_capability` | metadata withheld, so any requirement drops every route | **real metadata**; `tools` enforced; `streaming` waived (D7) |
| `_apply_candidate_pool` | skipped (metadata withheld) | runs on the **single pinned route**: its hard drops (unmapped, capability, opaque, policy, health) apply, Top-K cannot starve a set of one |
| `gate_admit_prove_confirm` (fresh passport, else one bounded confirm) | runs | same, on the pinned route |
| `_estimate_candidate_context_plans` | runs | same (estimate capped at 16,384 tokens: denies a route whose window is under about 18K tokens, never a route for request size alone) |
| `EligibilityGate` | runs when attached | same |
| `apply_best_of_admitted` ranking | runs | skipped (one route) |
| pin in `_decision_from_admit` **[#1299]** | after the ladder | removed; the lane already narrowed |
| cheap-path pack build and the `task_instructions_omitted` denial | runs | **skipped** |
| proof mint (`_chosen_route_proof`) | on the chosen route | on the pinned route; needs a receipt id and the profile digest |
| pack injection at the relay | when the decision has the `cheap_path_context_pack` flag | **not injected**: the lane's decision never carries the flag |

Details the table leaves out:

- **Metadata is required.** The lane loads the store with the existing `_load_metadata()`. If it
  cannot (missing or unreadable), the pinned route is denied with the named drop the capability gate
  already uses (`required_unknown`, "Core metadata store unavailable"). Reasons: `tools` is always
  required for OpenCode, the pool's unmapped check needs it, and the context-plan estimate falls
  back to a 16K window without it. `_load_metadata()` already runs on every default request that
  reaches that step (its result feeds the context-plan estimate; only the gate and the pool are
  denied it), so the lane adds no read.
- **Pool on one route.** The route is passed to `build_candidate_pool` as a one-element universe
  with the lane's *gate profile* (D7). Its hard drops are retained exactly; its Top-K and diversity
  steps are inert for one route. This is what makes "every per-route hard gate runs" literal: the
  unmapped drop, which `gate_capability` does not apply when there are no requirements, still
  applies.
- **Confirm probe.** A pinned route without a fresh passport gets the existing bounded confirm
  (ADR-012, consented, live only) exactly as an unpinned route would; with no confirm transport the
  gate drops it `confirm_unavailable` (or `no_passport`). The lane never adds a probe and never
  skips one. A request that fails authentication, the `model` check or the worker-mark check never
  reaches the ladder, so such a request triggers no probe either.
- **No execution inside `route()`.** Serve builds the service with `execute_offload=False`; the
  lane never executes the offload. The relay makes the one dispatch.
- **Receipt id.** With no pack there is no pack digest or prompt digest. The lane's receipt id is
  `vrct_` plus the first 24 hex characters of the SHA-256 of
  `route_id | profile digest | run_id | node_id | "worker-assignment"`. `_chosen_route_proof` mints
  nothing without a receipt id and a profile digest; both are always set on the lane. (A repeated
  identical request gets the same id, as an unpinned request does today.)
- **Recorded tier.** The decision records no computed escalation (`escalated` false, no reason,
  `protected` false) and the tier that `evidence._TIER_TO_POLICY_FLOOR` maps to `best_effort` (3),
  so evidence does not claim a floor the lane never computed.
- **Explicit protection is honoured, never lowered.** Today `criticality: critical` or
  `protected: true` (the relay turns both into `context["protected"]`, and `route()` also receives
  `criticality` as an argument) makes the legacy path skip offload entirely (`final_tier == 0` takes
  the primary model, never an offloaded route). A pin cannot carry that, and the lane bypasses the
  tier calculation that enforces it, so the lane checks it first: an assigned request that is
  explicitly protected is denied before the ladder (reason "protected work is never offloaded to an
  assigned route", safety flag `worker_assignment_protected_denied`, `protected` true on the denied
  decision), with no proof and no upstream call. This is separate from keyword parity (decision 6,
  which is about scanning node text): it is the explicit field, which only ever raises protection.
  Design choice that follows from "serve never lowers a floor"; a worker never sends these fields.
  Because protected requests never reach the lane's admitted path, an admitted lane decision has
  `protected` false truthfully.
- **Safety flags.** `free_tier_active_admit`, `prove_confirm_admit`, `capability_hard_gate` when a
  requirement exists, and `worker_assigned_lane`. Never `cheap_path_context_pack`.
- **What still reads the text, and why it is not a classifier.** The profile digest hashes the
  text, which binds the proof to this exact request. The context-plan estimate uses the text's
  token size, a per-route hard gate that can only remove the route; its family allowance comes from
  the constant family `agentic` (D6), so no keyword in the text changes whether a route fits.
  Neither selects, ranks or refuses a route by what the text means.
- **Keyword parity (decision 6).** The default `direct-gateway` worker never reaches serve and the
  orchestrator never runs the scanner: its floor is `required_capability_tier(node)`, from the
  node's risk and kind. An assigned request gets the same: no keyword protection on node text. The
  residual gap is the one the default path already has: a low-risk node whose text mentions money
  gets no keyword floor. The orchestrator's floor chose the route before signing; serve re-admits
  the pin at request time with hard gates only. Serve never lowers a floor. It drops only its own
  second, text-based guess.

### D6. One profile helper, one digest, called identically on both sides

The relay recomputes the profile with `prepared_task_profile(task, authority_context, planner=...)`
and compares the proof scope, which binds the profile digest. If the lane computed its digest any
other way (skipped the planner, or waived `streaming` before hashing) every assigned request would
be denied "proof scope or observation mismatch". So:

- `prepared_task_profile` gains an `assigned` mode. In it the planner is **not** consulted (no text
  capability inference), the requirements are `derive_requirements` over request fields only, and
  the profile's task family is the constant `agentic`, not inferred from text and not taken from a
  `task_family` field in the body (design choice: a worker is an agent with tools, and `agentic`
  carries the largest context-plan allowance, so the size gate errs toward failing closed). The
  digest therefore reflects what the request asked for, `streaming` included.
- `_offload_free_tier` (lane) and `_relay_completion` both call it with the same inputs: the same
  task text, the same `authority_context`, `assigned=True`. The relay passes `assigned=True`
  exactly when it holds a verified assignment. There is no second implementation.
- The waiver (D7) never touches the digest. It is applied to a **gate profile**, a copy of the
  profile whose `required_capabilities` lacks the waived names, handed to `gate_capability` and to
  `build_candidate_pool` (which reads its requirements from the profile, not from the tuple given
  to the gate, and would otherwise re-drop on `streaming`). The proof scope uses the digest view.

### D7. Capability gate: metadata for `tools`, a recorded waiver for `streaming` (decision 5)

- The lane passes the real metadata and identity map to `gate_capability` and the pool. `tools`
  stays required whenever the request carries `tools`: a route that lacks the capability, or has no
  metadata record, is dropped (`capability_mismatch`, `unmapped`, `required_unknown`).
- `streaming` is dropped from the gate requirements for a verified assignment, whichever request
  field raised it (`stream: true`, `streaming_required`). It is not dropped for any other request.
- **Recorded.** `FreeTierAdmitReceipt` gains two fields, both in `to_dict()`:
  `requirement_waivers` (a list of `{requirement, reason: "verified_worker_assignment",
  run_id, node_id}`) and `worker_assignment` (`{route_id, run_id, node_id, expires_at,
  assignment_digest}`). Three places carry them, and the second and third do **not** pick them up
  by themselves: (1) the admit receipt on the decision (`to_dict()`); (2) the evidence record, whose
  admit-receipt fields are an allowlist (`evidence._COMPACT_ADMIT_RECEIPT_FIELDS`, so serve
  persistence cannot silently drop fields), to which both names are added; (3) the persisted
  routing receipt, whose free-form `extensions` the lane fills from the same two values. The
  safety flag `worker_assignment_streaming_waived` is set when a waiver applies.
- **Class not computed.** The lane never classifies the request, so it emits no `worthiness_*`
  safety flag; the receipt's `task_class` says it was not computed (reason
  `worker_assignment:text_classification_skipped`). The build traces every consumer of
  `task_class` (the routing receipt reads it) and picks a value none rejects; no consumer on the
  lane branches on `ordinary` or `worthy`.
- A route that does not actually stream fails upstream and recovery reroutes it (research option
  S1). Stream evidence (ADR-013 probes, ADR-012 consent) is a later story.
- **Boundary:** serve's receipts are the receipts this change can write. The run-level
  `receipt.json` belongs to the orchestrator. Surfacing the waiver there is not one of the five
  stories; see "Open points for the operator".

### D8. Pin-first narrowing and the invariants (decision 7)

Narrowing to the pin before the Top-K and confirm steps removes only candidates; it adds none.
The assigned lane's admitted set is `{pin} ∩` (what `admit_free_tier_active` and cooling left), then
each later gate can only remove it. The two budget-limited steps (Top-K, confirm budget) cannot
starve a set of one; every per-route gate in those steps still applies to it.

**Invariant tests that pass unchanged** (they exercise `verdict/admission.py`, which the lane
neither calls nor changes; the relay's optional `AdmittedSet` check in `build_attempts` is also
unchanged and still applies to an assigned request when `VERDICT_RELAY_ADMISSION_CACHE` is on):

- `tests/test_admission.py::test_narrowing_only_removes_and_never_readmits` (admission before
  ranking, never re-admit);
- `tests/test_admission.py::test_unknown_runtime_stays_explicit_never_healthy`;
- `tests/test_admission.py::test_opaque_routes_are_never_admitted`;
- and, untouched, `tests/test_serve_capacity_order.py::test_route_name_never_makes_a_route_free`
  and `::test_route_name_never_makes_a_route_paid` (the lane never classifies a route by name).

Because those tests do not exercise the lane, **new tests carry the lane's own guarantee** (tasks
5.2 and 5.6): a pinned route outside the admitted set, a cooled route and an unknown-health route
are each **denied** with zero upstream calls and no proof minted; an `auto/*` route, an unmapped
route and a route absent from the inventory are denied; and a property test that for the same
snapshot the lane's admitted set is a subset of the unpinned ladder's active set, so nothing is
re-admitted. "Unknown health" is tested through both mechanisms the lane keeps: no fresh passport
with no confirm transport (`no_passport` or `confirm_unavailable`) and the `EligibilityGate`'s
`runtime_truth_absent`.

### D9. Receipts and secrets

Serve's receipts and evidence carry run id, node id, route id, expiry and `assignment_digest`.
They never carry the token, the signature or the key. Logging has two cases: when verification
**fails** nothing in the token can be trusted, so the log line carries the reason code only; after
verification **succeeds**, a later refusal (a `model` mismatch, for example) may carry the verified
route, run and node ids. Neither case ever logs the token, tag or key.

The guarantee is about transport credentials, not request content. The bearer and the key are never
copied into any serve output (responses serve itself writes, evidence, receipts, logs, and the
upstream request's headers; the proxy builds those from its own gateway key). Message content a
client chooses to send is forwarded verbatim, as every relay request is, and what an upstream echoes
back is outside this guarantee; no test puts the token in message content and expects redaction.

## Supersedes

The open change `opencode-worker-adapter` (merged in #1299, not archived) describes the header
pin. After this story these statements are false and the build edits that change (task 7.2):

| `opencode-worker-adapter` requirement or scenario | Now |
|---|---|
| "Worker is pinned to an admitted route other than serve's own pick" (header and `model`) | superseded: the pin comes from the verified assignment (D1, D5) |
| "Worker pin to a route outside the admitted set is denied" | kept in substance, restated for the assignment; the lane denies at narrowing |
| "Worker request without a valid model fails closed" | superseded: a `model` differing from the signed route is denied (D3) |
| "A worker request carrying an execution-path request is rejected" | kept, keyed on the verified assignment |
| "A worker request never falls back to another route" | kept |
| "Non-worker requests cannot pin a route" | kept |
| Design, "Worker identity" and the credential paragraph (serve token as worker credential) | superseded for serve; the executor side changes in #1325 |
| ADR-053 decision 1 implementation notes (`WorkerAssignedRoute`, header) | dated amendment (task 7.1) |

## Risks / Trade-offs

- **A bearer credential in the worker's hands.** Anyone holding an unexpired assignment can call
  its one route until `exp`. Bounded by one route, one run and node, and a short expiry; not
  single-use (see D1). A hostile shell can also read the key file (D4). Accepted (ADR-053
  decision 4).
- **Metadata becomes mandatory on the lane.** A route with no store record, or an unreadable
  store, is denied. Fail-closed is the point, but it can turn a route the default ladder would admit
  into a denial. The live smoke in #1325 is the check; the research found all three routes the
  smoke admitted map to a record.
- **A route with no known context window is denied for a large request.** Without a window the
  estimate falls back to the 16K default and cannot fit a large estimate (`context_plan_budget_
  insufficient`). Existing behaviour, kept.
- **The context-plan estimate never checks a large request against a mid-size window.** It caps at
  16,384 tokens, so a 130 KB request on a 32K-window route passes this step and fails upstream; the
  recovery path reroutes it. Not changed here.
- **Streaming waived.** A route that cannot stream is admitted for a streaming request and fails
  upstream. Recorded in the receipt; stream evidence is a later story.
- **The lane differs from the unpinned ladder in two budget-limited steps** (Top-K, confirm
  budget). Tests must show each per-route gate in those steps still denies (tasks 5.2 and 5.6).
- **Rotation is not graceful.** One key, no overlap: in-flight attempts fail and reroute.
- **#1299 executor is refused until #1325.** It sends the header and the serve token and no
  assignment. Opt-in executor; no working path regresses.
- **Every request re-runs the ladder.** A pin does not cache admission, so a passport that
  expires mid-attempt denies the next request (the attempt reroutes). Same cost as today's relay
  requests.
- **Wire format is mine.** `vwa1.<payload>.<tag>` and its fields are a design choice. #1325 builds
  the signer against the spec scenarios, and the live smoke there proves OpenCode forwards the
  bearer unchanged.

## Security / trust implications

- **Authentication.** HMAC-SHA256 with a 32-byte generated key; `hmac.compare_digest` on the tag;
  the payload is decoded only after the tag matches; generic 403 for every verification failure so
  the caller learns nothing about which check failed; length cap before decoding.
- **Authority.** The assignment authorises one route, for one run and node, on two relay paths.
  The worker holds no serve token, so it cannot reach `/v1/route`, `/v1/runs`, receipts, guidance or
  models, and cannot choose a route by header or by `model`.
- **No upgrade of the request.** The lane skips only serve's *text-based second guess*. It never
  lowers a floor, never re-admits, and every per-route gate that protects against an unhealthy,
  cooled, unmapped, opaque or incapable route still runs.
- **Secrets.** The token is a credential: never logged, never in receipts, evidence, errors or the
  forwarded request (`UpstreamProxy._headers` builds the upstream `authorization` from the
  proxy's own gateway key, so the incoming bearer is not forwarded today; a test asserts the
  upstream request carries the gateway's credential only and never the assignment).
  The key is never in an environment variable, a config file the worker reads, or an argument.
- **Production profile.** Unsupported for workers and fails closed (decision 7).
- **Residual risk.** Same-user file read of the key by a hostile shell (D4); replay of a captured
  assignment until `exp` for its one route (D1).

## Routing / context / memory implications

- **Routing.** A new lane in `_route` for verified assignments only; the unpinned ladder, the
  execution-path flow and `/v1/route` are unchanged. The assignment narrows, never widens: the
  orchestrator chose the route; serve proves it still passes every hard gate.
- **Context.** The lane builds no pack and injects none: the worker's request body is forwarded
  verbatim except that `model` is set to the route (equal to the signed route by D3) and
  Verdict-local fields are stripped as for every relay request. The context-plan estimate still
  runs as a route gate. Context for the worker (instructions file, short prompt) is #1327.
- **Memory.** None. No Hindsight or memory-plane interaction.

## Migration / rollback

- **Deploy order.** Merge, reinstall main, run `verdict setup --apply --allow host.serve` (writes
  the key file and restarts serve if the unit or env file changed; the key file alone needs no
  restart). Until the key exists serve answers 503 to assignment requests and works as before for
  everything else.
- **Existing behaviour.** Non-worker relay traffic, `/v1/route`, execution-path requests,
  `direct-gateway` and `prime-headless` workers are unchanged. The **[#1299]** OpenCode executor is
  refused until #1325 (opt-in, already failing).
- **Rollback.** Revert the change. The key file is inert without the verifier; the build makes
  the serve uninstall path remove it together with `serve.env`. There is no stored state to
  migrate: the header pin held none.
- **Order of the five stories.** This change is safe alone: nothing sends an assignment until
  #1325. #1325 depends on the format here; #1326, #1327 and #1328 depend on #1325.

## ADR impact

- Amends [ADR-053](../../../docs/adr/ADR-053-opencode-worker-adapter.md) decision 1 (a dated
  amendment, task 7.1): the route pin comes from a signed assignment, the header selects nothing,
  and the credential the worker holds is the assignment. Also notes `docs/decisions/LOG.md` D38.
- No new ADR: the separate signing key and the assigned lane implement decisions the operator
  already made on #1313. If the operator wants the key and format as an ADR of their own, say so.
- Invariants: no ADR 0001 invariant changes; the three `AGENTS.md` admission invariants are
  untouched (D8).

## Hand-offs to the later stories (not tasks here)

- **#1325** adds `mint_assignment` to `verdict/worker_assignment.py` against the format above
  (it has no production caller until the executor calls it, so it lands there, not here; story 1
  tests sign with an independent helper in `tests/`, which also checks the format rather than the
  verifier against itself), sets `exp` from the attempt timeout, puts the assignment in the child
  environment under the key variable instead of the serve token, uses the real serve token for
  `_check_serve`, and carries run and node ids. Its live smoke is the proof that OpenCode forwards
  the bearer unchanged.
- **#1326** checks the origin serve forwards to; serve exposes no upstream origin today.

## Proposed additions (beyond the eight decisions; the operator may drop any)

1. **`iat` and a maximum lifetime.** Add `iat` to the payload and refuse an assignment whose
   `exp - iat` exceeds 1,800 s (twice the 900 s attempt ceiling in `orchestration/runtime.py`), so a mis-signed
   long-lived assignment cannot exist. Not in the spec; if accepted, one field and one check. (The
   fixed bound on `exp` in D1 is a different thing: it only keeps `exp` representable as a
   timestamp and is part of the format here.)
2. **Rotation flag.** `verdict setup ... --rotate-assignment-key` instead of delete-and-rerun. Not
   proposed: more surface for no new capability.

## Open points for the operator

- **Where the streaming waiver shows in the run receipt.** Decision 5 says the waiver is recorded
  in the receipt. The receipts this change can write are serve's (the admission receipt, the
  routing receipt, the evidence record). The orchestrator's `receipt.json` would need to read the
  waiver back from serve per node; that is not one of the five stories. Tell me if it should be
  added to #1325.
- **Hindsight (searched 2026-11-24).** An earlier record says serve "pins requests to the assigned
  route regardless of the worker's `model`". The operator's later decision on #1313 (decision 3:
  deny) replaces it and this design follows the decision.
