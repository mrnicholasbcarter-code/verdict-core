# Proposal: verify the route OmniRoute actually served

## Why

Verdict picks one concrete route per attempt, but it did not check which route
served the answer. The direct gateway worker took the body `model` field and
defaulted it to the selected route when the field was missing, so a silent
substitution or a missing identity could be recorded as a match. The relay read
only the body `model` field, which some gateway paths rewrite.

OmniRoute 3.8.53 already reports the serving identity on every completion
response (`src/domain/omnirouteResponseMeta.ts`,
`src/sse/handlers/chatHelpers.ts`):

- `X-OmniRoute-Provider`: the provider alias that served the request.
- `X-OmniRoute-Model`: the provider-local model id that served it.
- `X-OmniRoute-Decision`: `strategy=<name>; provider=<alias>; latency_ms=<n>`,
  with `single` as the strategy for a non-combo request.
- `X-OmniRoute-Selected-Connection-Id`: the provider connection that was used.

## What Changes

- New `verdict/served_identity.py` reads those headers and compares them with
  the selected route. The result is `verified`, `mismatch`, `unknown` or
  `opaque`. Only an exact provider and model match is `verified`. Missing,
  repeated or malformed headers, and a selection with no provider prefix, are
  `unknown`. `auto/*` and `combo/*` selections are `opaque`: the served member
  is recorded but proves nothing.
- The direct gateway worker records the check on every attempt, also for
  streamed answers. A `mismatch` fails the attempt as `model_mismatch`, which
  reroutes, cools the route and revokes the dispatch permit. A missing body
  model is no longer replaced by the selected route.
- The run receipt records the served identity per attempt (provider, model,
  connection, decision strategy, status). It classifies the attempt identity
  from the check instead of comparing strings. Attempts without the check keep
  their old classification, so committed receipts still verify.
- The relay builds its observed route from the headers when they are present.
  A header mismatch is an `identity_mismatch` failure. Headers that are present
  but unusable leave the route unattested. The offload send fails on a header
  mismatch.

## Not in this change

- The Prime harness worker path cannot see gateway response headers. Its served
  identity stays what Prime reports, and the receipt shows it as not checked by
  headers.
- OmniRoute does not report the effort that was applied. Executed effort stays
  `unverified` in spawn receipts.
- Webhook and `/api/usage/analytics` cross-checks of the served identity.
