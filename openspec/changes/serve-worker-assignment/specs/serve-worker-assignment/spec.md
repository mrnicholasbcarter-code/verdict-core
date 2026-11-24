# Spec Delta

Facts that come from code merged in pull request #1299 are marked **[#1299]**. Requirement names
here do not repeat the requirement names of the open change `opencode-worker-adapter`; the design
lists which of that change's scenarios this one supersedes.

## ADDED Requirements

### Requirement: A signed assignment carries the route, the run, the node and an expiry

A worker assignment SHALL be an opaque bearer string `vwa1.<payload>.<tag>`, where `payload` is the
base64url of a JSON object with exactly the fields `route_id`, `run_id`, `node_id` (each a
non-empty string of at most 128 characters without control characters) and `exp` (an integer of
seconds since the Unix epoch, UTC), and `tag` is the base64url of
`HMAC-SHA256(key, "verdict-worker-assignment/v1\n" + payload)`. A token over 2 KiB, a token that
does not split into exactly three parts, a payload that is not that object, and an `exp` that is
not an integer (including a boolean or a float) SHALL be refused.

#### Scenario: A well-formed, unexpired assignment verifies

- **WHEN** an assignment signed with the installed key, `exp` in the future, is verified
- **THEN** verification yields the route, run, node and expiry it carries

#### Scenario: Malformed assignments are refused

- **WHEN** the token has two parts, four parts, a tag of the wrong length, a payload that is not
  base64url, a payload that is not a JSON object, an extra field, a missing field, an empty
  `route_id`, or an `exp` that is a string, a float, a boolean or negative
- **THEN** verification is refused and the request is denied with no upstream call

#### Scenario: An oversized token is refused before it is decoded

- **WHEN** the bearer is longer than 2 KiB
- **THEN** it is refused without decoding it

### Requirement: An assignment is verified with a constant-time compare and denies on any failure

Serve SHALL recompute the tag over the received payload text with the installed key and compare it
with `hmac.compare_digest`, SHALL decode the payload only after the tag matches, and SHALL treat an
assignment whose `exp` is not strictly after the current UTC time as expired. A forged, expired,
field-tampered, wrong-key or truncated assignment SHALL be denied with HTTP 403 and the same body
as any wrong bearer, with no routing and no upstream call. The caller SHALL NOT be told which check
failed; serve SHALL log the reason code only, never the token.

#### Scenario: A forged tag is denied

- **WHEN** a request presents an assignment whose tag was made with a different key
- **THEN** it is denied 403, nothing is routed, no proof is minted and upstream is never contacted

#### Scenario: Each signed field is covered by the tag

- **WHEN** the payload of a valid assignment is changed in `route_id`, `run_id`, `node_id` or `exp`
  and the tag is kept
- **THEN** each change is denied 403 with no upstream call

#### Scenario: An expired assignment is denied

- **WHEN** a request presents an assignment whose `exp` equals or precedes the current time
- **THEN** it is denied 403 with no upstream call, including a request made late in an attempt
  whose earlier requests were admitted

#### Scenario: The tag is compared in constant time

- **WHEN** an assignment is verified
- **THEN** the tag comparison is `hmac.compare_digest`, and the payload is not decoded before the
  comparison succeeds

#### Scenario: A failure never reveals its cause or the token

- **WHEN** the assignment is forged, expired or malformed
- **THEN** the response body is the generic authentication-failed body, and neither the response,
  the evidence nor the log contains the token

### Requirement: The assignment key is a separate generated key that fails closed

The signing key SHALL be a separate secret of at least 32 random bytes, never the serve bearer
token. `verdict setup --apply --allow host.serve` SHALL generate it when absent, keep an existing
valid key, and write nothing on a dry run. It SHALL be stored only in
`~/.config/verdict/worker-assignment.key` (mode 0600, written atomically), SHALL be read from that
file on every assignment verification and never cached across requests, and SHALL NOT be placed in
any process environment, the credentials store, `serve.env` or a worker's configuration. When the
file is missing, unreadable or invalid, serve SHALL answer 503 to an assignment request and admit
nothing; requests that do not present an assignment SHALL be unaffected. `verdict doctor` SHALL
report a missing, short or wrongly-moded key file. Rotation SHALL be removing the file and running
setup again; assignments signed with the old key SHALL stop verifying at once.

#### Scenario: Setup generates the key once

- **WHEN** setup runs with no key file, and then runs again
- **THEN** the first run writes a 0600 file of at least 32 random bytes and the second keeps it
  unchanged

#### Scenario: A dry run writes nothing

- **WHEN** setup runs as a dry run with no key file
- **THEN** no file is written

#### Scenario: A missing key fails closed

- **WHEN** an assignment request arrives and the key file is missing, empty, shorter than 32 bytes
  or not decodable
- **THEN** serve answers 503 and no upstream call is made, while a request with the serve token and
  no assignment is served as before

#### Scenario: The key is never in an environment

- **WHEN** serve verifies an assignment and the orchestrator loads the key
- **THEN** no environment variable of either process holds the key, and `serve.env` and the
  credentials store do not contain it

#### Scenario: Rotation takes effect on the next request

- **WHEN** the key file is replaced after an assignment was signed with the old key
- **THEN** the next request with that assignment is denied 403, and one signed with the new key
  verifies

#### Scenario: Doctor reports a bad key file

- **WHEN** the key file is missing, shorter than 32 bytes, or has a mode other than 0600
- **THEN** `verdict doctor` names the problem for the serve unit and never prints the key

### Requirement: An assignment is honoured only on the OpenAI relay surfaces

Serve SHALL accept an assignment, as the `Authorization: Bearer` value, only on
`POST /v1/chat/completions` and `POST /v1/responses`. On any other path or method, on
`POST /v1/messages`, and in `x-api-key`, an assignment SHALL be refused with 403. A request whose
bearer equals the serve token SHALL be authenticated exactly as before. In anonymous mode serve
SHALL verify no assignment.

#### Scenario: Off the relay path an assignment is refused

- **WHEN** a valid assignment is presented to `GET /v1/guidance/status`, `POST /v1/route`,
  `GET /v1/runs`, `GET /v1/models`, or `POST /v1/messages`, or as `x-api-key` on `/v1/messages`
- **THEN** each is refused 403 and nothing is routed

#### Scenario: The serve token still authenticates

- **WHEN** a request presents the serve token and no assignment and no worker header
- **THEN** it is authenticated and routed as before this change

#### Scenario: Anonymous mode grants no assigned lane

- **WHEN** serve runs with `LLMGATE_ALLOW_ANONYMOUS` and no token, and a request carries an
  assignment or the worker header
- **THEN** it is denied 403 with no upstream call

### Requirement: The bare worker header selects nothing

A request that carries the `X-Verdict-Worker` header, or a bearer with the `vwa1.` prefix, and that
was not authenticated with a verified assignment SHALL be denied 403 before any routing. **[#1299]**
The header alone, with the serve token, SHALL no longer pin a route; `WorkerAssignedRoute` and its
relay and admission handling SHALL be removed. A verified assignment SHALL assign whether or not the
header is present.

#### Scenario: Header plus the serve token pins nothing

- **WHEN** a request carries the serve token, the `X-Verdict-Worker` header and a `model` naming an
  admitted route
- **THEN** it is denied 403, nothing is routed, no proof is minted and upstream is never contacted

#### Scenario: Header with nothing else

- **WHEN** a request carries the worker header and no valid authentication
- **THEN** it is refused as any unauthenticated request is refused

#### Scenario: A verified assignment needs no header

- **WHEN** a request carries a verified assignment and no `X-Verdict-Worker` header
- **THEN** it is routed on the assigned lane

#### Scenario: An execution-path request is still refused

- **WHEN** a request with a verified assignment carries `execution_path_request`, whatever its
  value
- **THEN** it is refused 400 before any routing, and `IntelligenceService.route` independently
  refuses the combination

### Requirement: A model that differs from the signed route is denied

For a verified assignment the request's `model` SHALL equal the signed `route_id` by exact string
comparison. A missing, null, empty, non-string or different `model` SHALL be denied through the
relay's existing denial path (the decision is `denied` and evidence records it), never ignored and
never rewritten, with no upstream call. After routing the relay SHALL verify that the decision's
route equals the signed route and SHALL bound the request to exactly that route, with no
alternatives.

#### Scenario: A different model is denied

- **WHEN** a valid assignment names route A and the request's `model` is route B, including a
  route that is admitted
- **THEN** the request is denied, evidence records the denial, and upstream is never contacted

#### Scenario: A missing or malformed model is denied

- **WHEN** the request's `model` is absent, null, an empty string, a number or a list
- **THEN** each is denied with no upstream call

#### Scenario: The matching model is served on exactly one route

- **WHEN** the request's `model` equals the signed route and the route passes its gates
- **THEN** one attempt is built on that route, with no alternatives and no fallback

### Requirement: The assigned lane narrows to the pinned route first and keeps every per-route gate

For a verified assignment, `IntelligenceService._route` SHALL take an assigned lane placed after the
`require_authority` raise. The lane SHALL skip the keyword scan, the text classification
(`classify_worthiness`), the planner's capability inference, the advisory signals, the Top-K
shortlist and the cheap-path pack and its injection. It SHALL narrow the admitted set to the pinned
route immediately after the active and cooling steps and deny when the route is absent, naming the
check that dropped it. It SHALL then run on that route every per-route hard gate: the capability
gate with real metadata, the candidate pool's hard drops, the fresh-passport or bounded-confirm
gate, the context-plan estimate and the `EligibilityGate`, and SHALL mint the route-bound proof with
the same function as for a chosen route. The lane SHALL NOT re-admit any route; its admitted set
SHALL be a subset of `{pinned route}` intersected with what the unpinned ladder would have left
after its active and cooling steps. When the lane cannot obtain live admission it SHALL deny and
SHALL NOT fall through to the legacy path.

#### Scenario: A pinned route outside the admitted set is denied

- **WHEN** the signed route is absent from the inventory, is an opaque route such as `auto/best`,
  or belongs to a provider that is quota-blocked or inactive
- **THEN** the request is denied with the reason of the check that dropped the route, no proof is
  minted, upstream is never contacted, and no other route is selected

#### Scenario: A cooled route is denied

- **WHEN** the pinned route is negative in the health cache
- **THEN** the request is denied `route_cooling`, with no proof and no upstream call

#### Scenario: A route of unknown health is denied

- **WHEN** the pinned route has no fresh passport and no confirm transport can confirm it, or the
  `EligibilityGate` excludes it as `runtime_truth_absent`
- **THEN** the request is denied with the named drop, with no proof and no upstream call

#### Scenario: An unmapped route is denied even with no capability required

- **WHEN** the pinned route has no metadata record and the request needs no capability
- **THEN** the candidate pool's hard drop denies it

#### Scenario: Nothing is re-admitted

- **WHEN** the same snapshot is routed once unpinned and once assigned to each route in it
- **THEN** every route the lane admits is also in the unpinned ladder's set after its active and
  cooling steps, and the lane admits at most the one pinned route

#### Scenario: The sixth of six candidates is admitted

- **WHEN** the pinned route would rank sixth of six under the Top-4 shortlist and passes every
  gate
- **THEN** it is admitted

#### Scenario: Production profile still fails closed

- **WHEN** serve runs in the production profile, or `VERDICT_REQUIRE_EXECUTION_PATH` is on, and a
  request carries a verified assignment
- **THEN** the lane is not reached, the request is refused 400 as an execution-path error, and
  nothing is routed

### Requirement: The assigned lane does not second-guess the request from its text

On the assigned lane the request text SHALL NOT select, filter or refuse a route: the keyword
scanner, the worthiness classification and the planner's text-derived capabilities SHALL NOT run.
The request body SHALL be forwarded verbatim except that `model` is the pinned route and
Verdict-local fields are stripped as for every relay request: no pack SHALL be built and no message
added. The lane's decision SHALL NOT carry the `cheap_path_context_pack` flag. Keyword protection
for an assigned request is parity with the default `direct-gateway` path: none on node text, with
the floor chosen by the orchestrator before it signs.

#### Scenario: Words that trip the scanner do not refuse the worker

- **WHEN** an assigned request's text contains "payment" or "credential" and its pinned route
  passes its gates
- **THEN** it is admitted and served, and its receipt does not name an escalation

#### Scenario: A large request is not capped by the pack

- **WHEN** an assigned request is 130,528 characters (or any size) and the pinned route's context
  window is large enough for the context-plan estimate
- **THEN** it is admitted, where the unpinned ladder denies at 34,000 characters

#### Scenario: The body is forwarded verbatim

- **WHEN** an assigned request with two messages is admitted
- **THEN** the upstream request has the same two messages, and its `model` is the pinned route

#### Scenario: A route with a small window is still denied

- **WHEN** the pinned route's context window is under the context-plan estimate's minimum
- **THEN** it is denied `context_plan_budget_insufficient`

### Requirement: The capability gate has metadata on the lane; tools is required, streaming is waived and recorded

On the assigned lane the capability gate and the candidate pool SHALL receive the real metadata
store and identity map. `tools` SHALL stay required whenever the request carries `tools`: a route
without the capability, without a metadata record, or with no metadata store available SHALL be
dropped and the request denied. The `streaming` requirement SHALL be waived for a verified
assignment only, whichever request field raised it. The waiver SHALL be recorded as
`requirement_waivers`, and the assignment as `worker_assignment` (route id, run id, node id, expiry
and a digest of the payload, never the token), in serve's admission receipt, in the evidence record
and in the persisted routing receipt, and the lane's safety flags SHALL include
`worker_assignment_streaming_waived`. Streaming SHALL NOT be waived for a request without a
verified assignment.

#### Scenario: A request with tools and stream is admitted on a route with tools

- **WHEN** an assigned request has `tools` and `stream: true` and the pinned route's metadata
  records `tools` and has no streaming fact
- **THEN** it is admitted, and the admission receipt, the evidence record and the routing receipt
  record the streaming waiver with the run and node ids

#### Scenario: Tools is still enforced

- **WHEN** an assigned request carries `tools` and the pinned route's metadata says it does not
  support them, or has no record, or the store cannot be loaded
- **THEN** the route is dropped (`capability_mismatch`, `unmapped` or `required_unknown`) and the
  request is denied with no upstream call

#### Scenario: An unassigned request is not waived

- **WHEN** a request without an assignment has `stream: true` and the route has no streaming fact
- **THEN** the gate treats `streaming` as required exactly as before

### Requirement: Admission and the relay compute one task profile

The assigned lane and the relay SHALL compute the task profile with the same helper, in the same
assigned mode (planner not consulted, requirements from request fields only, `streaming` included
in the digest), over the same task text and request context. The streaming waiver SHALL be applied
only to the requirements handed to the capability gate and the candidate pool, never to the digest.
A proof minted by the lane SHALL verify at the relay.

#### Scenario: A lane-minted proof verifies at the relay

- **WHEN** an assigned request with `stream: true` and `tools` is admitted
- **THEN** the relay's recomputed profile digest equals the proof scope's, and the request is
  dispatched, not denied "proof scope or observation mismatch"

#### Scenario: Words in the text do not change the digest's requirements

- **WHEN** an assigned request's text contains "implement", "build", "api" or "test" and it carries
  no `tools`
- **THEN** `tool-calling` is not added to its requirements by either side

### Requirement: Serve's receipts record the assignment and never a secret

Serve's evidence and admission receipt for an assigned request SHALL carry the route id, run id,
node id, expiry and the payload digest, and SHALL NOT carry the assignment token, its tag or the
key. A denial log line SHALL carry the reason code only.

#### Scenario: Receipts name the run and node

- **WHEN** an assigned request is admitted
- **THEN** its admission receipt names the signed route, run id, node id and expiry

#### Scenario: A secret never appears

- **WHEN** a request is made with an assignment and the assignment, its tag and the key are searched
  for in the response, the evidence record, the admission receipt, the persisted routing receipt,
  the log output and the upstream request
- **THEN** none is found

### Requirement: Requests without an assignment are unchanged

A relay request that carries no assignment and no worker marker SHALL take the same ladder, the same
denials and the same forwarded body as before this change, including the keyword scan, the planner,
the Top-K step where it applies, the pack and its injection, and the streaming requirement. The
admission invariants in `tests/test_admission.py` SHALL pass unchanged.

#### Scenario: An ordinary relay request is as before

- **WHEN** a request with the serve token and no worker marker names a model and carries "payment"
  in its text
- **THEN** it is refused by the scanner exactly as today, and its `model` does not select the route

#### Scenario: The admission invariants hold

- **WHEN** `tests/test_admission.py::test_narrowing_only_removes_and_never_readmits`,
  `::test_unknown_runtime_stays_explicit_never_healthy` and `::test_opaque_routes_are_never_admitted`
  run
- **THEN** they pass without change
