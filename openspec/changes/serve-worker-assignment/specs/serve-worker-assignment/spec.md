# Spec Delta

Facts that come from code merged in pull request #1299 are marked **[#1299]**. Requirement names
here do not repeat the requirement names of the open change `opencode-worker-adapter`; the design
lists which of that change's scenarios this one supersedes.

## ADDED Requirements

### Requirement: A signed assignment carries the route, the run, the node and an expiry

A worker assignment SHALL be an opaque bearer string `vwa1.<payload>.<tag>`, where `payload` is the
unpadded base64url of a JSON object with exactly the fields `route_id`, `run_id`, `node_id` (each a
non-empty string of at most 128 characters without control characters or lone surrogates), `iat` (issued at) and `exp`
(expiry), each of the last two an integer of seconds since the Unix epoch, UTC, from 0 through
253402300799 (9999-12-31T23:59:59Z, the last second a UTC `datetime` can hold), and `tag` is the
unpadded base64url of `HMAC-SHA256(key, "verdict-worker-assignment/v1\n" + payload)`, with
`payload` taken as the received text. Base64url here is the URL-safe alphabet without `=` padding,
decoded strictly: any other character, an impossible length or non-canonical trailing bits SHALL be
refused. The lifetime `exp - iat` SHALL be from 0 through 1,800 seconds (twice the 900 s attempt
ceiling); a longer or negative lifetime SHALL be refused. `iat` SHALL NOT be more than 60 seconds
ahead of the verifier's clock, so the usable window of any accepted assignment is at most 1,860
seconds from now whatever its dates say (without this a token dated years ahead with a one-second
lifetime would be usable for years); a later `iat` SHALL be refused as `malformed`.
A token over 2 KiB, a token that does not split into exactly three parts, a payload that is not
that object, an `exp` or `iat` that is not an integer (including a boolean or a float), and an
`exp` or `iat` outside that range SHALL be refused. The verifier SHALL check the ranges before it
does arithmetic on them, SHALL turn `exp` into a UTC time with an explicit offset as part of
verification, and any `OverflowError`, `ValueError` or `OSError` raised by that conversion SHALL be
refused as `malformed`, never raised to the caller. A correctly signed assignment whose `exp` cannot
be represented therefore ends as the generic authentication refusal, never as an error response.

#### Scenario: A well-formed, unexpired assignment verifies

- **WHEN** an assignment signed with the installed key, `exp` in the future, is verified
- **THEN** verification yields the route, run, node and expiry it carries

#### Scenario: Malformed assignments are refused

- **WHEN** the token has two parts, four parts, a tag of the wrong length or with non-canonical
  trailing bits, a payload that is not base64url, a payload that is not a JSON object, a payload
  with a repeated key, an extra field, a missing field (`iat` included), an empty `route_id`, or an
  `exp` or `iat` that is a string, a float, a boolean or negative
- **THEN** verification is refused and the request is denied with no upstream call

#### Scenario: An exp that cannot be a timestamp is refused as malformed

- **WHEN** an assignment carries a correct tag over a payload whose `exp` is 253402300800, 2^63,
  2^64 or 10^30
- **THEN** verification is refused with the reason `malformed` (not `expired`, and no
  `OverflowError`, `ValueError` or `OSError` reaches the caller), and a request that presents it is
  denied 403 with the generic body, with no upstream call

#### Scenario: The largest representable exp verifies

- **WHEN** an assignment is signed with `exp` 253402300799 and an `iat` within 1,800 s of it, and
  verified with a clock inside that lifetime (for example 9999-12-31T23:50:00Z)
- **THEN** verification succeeds and its expiry is `9999-12-31T23:59:59Z`

#### Scenario: A token dated ahead of the clock is refused

- **WHEN** an assignment is signed with a correct tag, a lifetime of one second and an `iat` more
  than 60 seconds ahead of the verifier's clock (including `iat` 253402300798, `exp` 253402300799)
- **THEN** verification is refused with the reason `malformed`, and a request that presents it is
  denied 403 with the generic body, with no upstream call; an `iat` up to 60 seconds ahead is
  accepted

#### Scenario: An exp of zero is well-formed and expired

- **WHEN** an assignment is signed with `exp` 0 and `iat` 0
- **THEN** verification is refused with the reason `expired`, not `malformed`

#### Scenario: A lifetime over 1,800 seconds is refused

- **WHEN** an assignment is signed with a correct tag, `exp - iat` of 1801 (or more), whether or not
  `exp` is still in the future
- **THEN** verification is refused with the reason `malformed`, and a request that presents it is
  denied 403 with the generic body, with no upstream call

#### Scenario: A lifetime of 1,800 seconds or less is accepted

- **WHEN** an assignment is signed with `exp - iat` of 0, 900 or 1800 and an unexpired `exp`
- **THEN** verification succeeds

#### Scenario: A negative lifetime is refused

- **WHEN** an assignment is signed with `iat` after `exp`, or with a negative `iat`
- **THEN** verification is refused with the reason `malformed`

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

- **WHEN** the payload of a valid assignment is changed in `route_id`, `run_id`, `node_id`, `iat`
  or `exp` and the tag is kept
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
token. `verdict setup --apply --allow host.serve` SHALL generate it when absent or unusable, keep
an existing valid key (repairing only a mode other than 0600, never regenerating for it), refuse a
key path that is not a file, and write nothing on a dry run. It SHALL be stored only in
`~/.config/verdict/worker-assignment.key` (mode 0600, written atomically), SHALL be read from that
file on every assignment verification and never cached across requests (the read SHALL NOT run on
the serve event loop, so a slow home filesystem stalls only the request that waits on it), and
SHALL NOT be placed in any process environment, the credentials store, `serve.env` or a worker's configuration. When the
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

#### Scenario: A key-only change is reported and restarts nothing

- **WHEN** setup creates, replaces or repairs the mode of the key file while the unit and the env
  file are unchanged
- **THEN** its result reports `changed` true, `service_changed` false and no `try-restart` or
  `daemon-reload` (serve reads the key on each request), and the host step still restarts serve
  when it is unhealthy exactly as before

#### Scenario: An unusable key is replaced and a loose mode is only repaired

- **WHEN** setup runs and the key file is empty, shorter than 32 bytes or undecodable
- **THEN** it is replaced by a fresh key; but a valid key whose mode is not 0600 keeps its bytes
  and only has its mode set to 0600

#### Scenario: A missing key fails closed

- **WHEN** an assignment request arrives and the key file is missing, empty, shorter than 32 bytes
  or not decodable, or the read itself raises
- **THEN** serve answers 503 and no upstream call is made, while a request with the serve token and
  no assignment is served as before

#### Scenario: The key is never in an environment

- **WHEN** setup generates the key and serve verifies an assignment
- **THEN** no environment variable of the serve process holds the key (the loader never reads or
  writes the environment), and `serve.env`, the credentials store and the setup result do not
  contain it

#### Scenario: Rotation takes effect on the next request

- **WHEN** the key file is replaced after an assignment was signed with the old key
- **THEN** the next request with that assignment is denied 403, and one signed with the new key
  verifies

#### Scenario: Doctor reports a bad key file

- **WHEN** the key file is missing, shorter than 32 bytes, or has a mode other than 0600
- **THEN** `verdict doctor` names the problem for the serve unit and never prints the key

### Requirement: An assignment is honoured only on the OpenAI relay surfaces

Serve SHALL accept an assignment, as the `Authorization: Bearer` value, only on
`POST /v1/chat/completions` and `POST /v1/responses`. On every other authenticated path or method, on
`POST /v1/messages`, and in `x-api-key`, an assignment SHALL be refused with 403. The only
exceptions are `/health` and the OmniRoute webhook path, which skip caller authentication for every
credential, never read a bearer, and select and authorise nothing. A request whose bearer equals
the serve token SHALL be authenticated exactly as before; a serve with no token and no anonymous
mode answers 503 to every request, an assignment-bearing one included, as today.

An assignment-bearing request is one whose `Authorization` bearer value or `x-api-key` value has
the `vwa1.` prefix. In anonymous mode (`LLMGATE_ALLOW_ANONYMOUS` and no token) serve SHALL verify no
assignment and SHALL NOT load the key, and SHALL refuse every assignment-bearing request with 403,
on every path and method, before the anonymous request is passed on. A request without an
assignment passes in anonymous mode as before, and a non-loopback client keeps its existing 403.

#### Scenario: Off the relay path an assignment is refused

- **WHEN** a valid assignment is presented to `GET /v1/guidance/status`, `POST /v1/route`,
  `GET /v1/runs`, `GET /v1/models`, or `POST /v1/messages`, or as `x-api-key` on `/v1/messages`
- **THEN** each is refused 403 and nothing is routed

#### Scenario: The serve token still authenticates

- **WHEN** a request presents the serve token and no assignment and no worker header
- **THEN** it is authenticated and routed as before this change

#### Scenario: Anonymous mode refuses an assignment on every path

- **WHEN** serve runs with `LLMGATE_ALLOW_ANONYMOUS` and no token, and a loopback request carries
  an assignment bearer (or the prefix in `x-api-key`) to `POST /v1/chat/completions`,
  `POST /v1/responses`, `GET /v1/guidance/status`, `POST /v1/route`, `GET /v1/runs`,
  `GET /v1/models` or `POST /v1/messages`
- **THEN** each is refused 403 with the generic authentication-failed body, nothing is verified,
  the key file is never read, nothing is routed and no upstream call is made

#### Scenario: Anonymous mode without an assignment is unchanged

- **WHEN** serve runs with `LLMGATE_ALLOW_ANONYMOUS` and no token, and a loopback request carries
  no assignment
- **THEN** it passes the middleware as before this change, and a non-loopback client is refused
  403 `anonymous mode is loopback-only` as before

#### Scenario: Anonymous mode grants no assigned lane through the header

- **WHEN** serve runs with `LLMGATE_ALLOW_ANONYMOUS` and no token, and a relay request carries the
  worker header and no assignment
- **THEN** it is denied 403 by the relay guard with no upstream call

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

#### Scenario: A non-ASCII assignment credential is refused, not an error

- **WHEN** a bearer or an `x-api-key` value with the `vwa1.` prefix holds a non-ASCII character,
  alone or beside another credential
- **THEN** it is refused 403 with the generic body and no upstream call, never a server error

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
comparison. A missing, null, empty, non-string or different `model` SHALL be refused with HTTP 400
before any routing, never ignored and never rewritten, with no ladder, no proof and no upstream
call; serve SHALL log the refusal with the reason code, the signed route id, the run id and the
node id, and never the token. After routing the relay SHALL verify that the decision's route equals
the signed route and SHALL bound the request to exactly that route, with no alternatives; a
decision for any other route SHALL be denied.

#### Scenario: A different model is refused

- **WHEN** a valid assignment names route A and the request's `model` is route B, including a
  route that is admitted
- **THEN** the request is refused 400 before routing, the refusal is logged with the signed route,
  run and node ids, and upstream is never contacted

#### Scenario: A missing or malformed model is refused

- **WHEN** the request's `model` is absent, null, an empty string, a number or a list
- **THEN** each is refused 400 before routing with no upstream call

#### Scenario: The matching model is served on exactly one route

- **WHEN** the request's `model` equals the signed route and the route passes its gates
- **THEN** one attempt is built on that route, with no alternatives and no fallback

#### Scenario: A decision for another route is denied

- **WHEN** routing returns a decision whose route is not the signed route
- **THEN** the decision is denied and no attempt is dispatched

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
SHALL NOT fall through to the legacy path. Before any ladder step the lane SHALL deny an
explicitly protected request (`protected: true`, or criticality critical): protected work is never
offloaded and a pin SHALL NOT lower that floor.

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

#### Scenario: An explicitly protected request is denied before the ladder

- **WHEN** an assigned request carries `protected: true`, or `criticality` critical (as the request
  field or as the argument to `route()`)
- **THEN** it is denied before any ladder step with `protected` true, the safety flag
  `worker_assignment_protected_denied`, no proof and no upstream call, and no route is selected

#### Scenario: A lane that cannot obtain live admission denies

- **WHEN** a verified assignment is routed by a service with no live admission (an offline
  service, or none configured), or its request profile cannot be built (for example an unknown
  `spend_policy`)
- **THEN** the request is denied with no proof and no upstream call, the legacy path and its
  scanner are not reached, and no other route is selected

#### Scenario: Production profile still fails closed

- **WHEN** serve runs in the production profile, or `VERDICT_REQUIRE_EXECUTION_PATH` is on, and a
  request carries a verified assignment
- **THEN** the lane is not reached, the request is refused 400 as an execution-path error, and
  nothing is routed

### Requirement: The assigned lane does not classify the request from its text

On the assigned lane the keyword scanner, the worthiness classification and the planner's
text-derived capabilities SHALL NOT run, so no request text SHALL select, filter or refuse a route
through them. Two things still read the text and are not classifiers: the task-profile digest (a hash of the
text, which binds the proof to the request) and the context-plan estimate (token size of the text
against the route's window, a per-route hard gate), whose task family is the constant `agentic` and
never inferred from the text. The request body SHALL be forwarded verbatim
except that `model` is the pinned route and Verdict-local fields are stripped as for every relay
request: no pack SHALL be built and no message added. The lane's decision SHALL NOT carry the
`cheap_path_context_pack` flag. Keyword protection for an assigned request is parity with the
default `direct-gateway` path: none on node text, with the floor chosen by the orchestrator before
it signs.

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

#### Scenario: Words in the text do not change the family

- **WHEN** two assigned requests differ only in words such as "review", "plan" or "docs" (the two
  texts may differ in length)
- **THEN** both profiles have the task family `agentic`, and the context-plan estimate for each
  uses the same family allowance, the `agentic` one

#### Scenario: Texts of the same length get the same estimate

- **WHEN** two assigned requests differ only in words such as "review", "plan" or "docs", the two
  texts have the same character length, and the requests carry the same `tools`, `stream` and
  criteria fields
- **THEN** their context-plan estimates are equal, because the estimate depends on the text only
  through its token size

### Requirement: Serve's receipts record the assignment and never a secret

Serve's evidence and admission receipt for an assigned request SHALL carry the route id, run id,
node id, expiry and the payload digest, and SHALL NOT carry the assignment token, its tag or the
key. When verification fails, serve's log line SHALL carry the reason code only; after verification
succeeds, a later refusal MAY also carry the verified route, run and node ids. No log line SHALL
carry the token, its tag or the key. These guarantees cover transport credentials, not request
content: message content a client sends is forwarded verbatim. The lane's receipt id, which is also
the persisted routing attempt id for an admitted decision, SHALL be derived from an unambiguous
(injective) encoding of the route id, the profile digest, the run id and the node id, so that
assignments whose ids differ, including ids that contain a delimiter character such as `|`, never
produce the same hash input.

#### Scenario: Receipts name the run and node

- **WHEN** an assigned request is admitted
- **THEN** its admission receipt names the signed route, run id, node id and expiry

#### Scenario: Ids that hold the delimiter characters keep distinct receipt ids

- **WHEN** two assignments for the same route and the same request are admitted with the run and
  node ids (`a|b`, `c`) and (`a`, `b|c`), and again with (`a","b`, `c`) and (`a`, `b","c`)
- **THEN** the lane's receipt ids differ within each pair, the persisted routing attempt ids
  differ with them, and the same assignment sent twice gets the same receipt id both times

#### Scenario: A secret never appears

- **WHEN** a request is made with an assignment as its bearer (and not in its body) and the
  assignment, its tag and the key are searched for in the response serve writes, the evidence
  record, the admission receipt, the persisted routing receipt, the log output and the upstream
  request's headers and body
- **THEN** none is found

#### Scenario: A failed verification logs the reason only

- **WHEN** an assignment fails verification
- **THEN** the log line holds the reason code and no field taken from the token

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
