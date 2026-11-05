# ADR-039: Split passive inventory authentication by API surface

- **Status:** Proposed
- **Date:** 2026-11-05
- **Related:** [ADR-007](ADR-007-omniroute-catalog-qualification.md), [ADR-011](ADR-011-omniroute-catalog-qualification-baseline.md), [ADR-012](ADR-012-consented-budgeted-probes.md)
- **Ticket:** BOD-345

## Context

Passive identify currently calls `fetch_omniroute_evidence(api_key=key)`. That sends one Bearer credential to all six bounded GET sources: five OmniRoute management `/api` endpoints and the OpenAI-compatible `/v1/models` endpoint. Installed OmniRoute 3.8.52 separates these authorities: its read Access Token authenticates management reads, while an inference API key is needed for `/v1/models`. Sending either credential to the wrong surface makes part of passive inventory fail and does not establish compatibility.

The existing supported credential resolver already establishes the inference-key precedence from environment, the owner-only credentials store, and supported owner-only compatibility locations. The proposal must reuse that resolver and the existing secure store; it must not create a new secret file. The obsolete `credentials_registry` `X-Management-Token` entry and `/admin/health` checker are not compatibility evidence.

## Decision

Propose that passive identify accept an optional `management_token` for `fetch_omniroute_evidence` and select credentials by endpoint family:

- Keep six fixed source slots and at most six GETs under the existing shared aggregate deadline.
- Use the resolved inference API key only for the single `/v1/models` GET. If it is absent, mark that source locally unavailable without network access; management reads proceed independently. If both credentials are absent, all six sources are locally unavailable.
- Resolve the management token explicitly from stripped `OMNIROUTE_MANAGEMENT_TOKEN` and the same-named field in the existing secure store only. Never use inference compatibility files, Prime auth, or the credentials registry for management authentication.
- Use the management token only for the five management `/api` GETs. When it is absent, retain the current resolved `api_key` as the management fallback, but never fall back after a 401 or other request failure.
- Do not retry a failed request with the other authentication family. Source errors remain source-specific and truthful.
- Resolve credentials through the existing secure environment/store path, with no new secret file and no credential logging or persistence. Existing permission checks are mode checks; this proposal does not claim ownership or symlink protection.

This is a proposal for implementation review, not an Accepted decision or an implementation claim.

## Preserved bounds and invariants

The change must preserve six fixed source slots and at most six bounded GETs: five management reads and `/v1/models`. It must issue no POST, write, inference/model-generation, or additional compatibility calls. Existing no-redirect behavior, per-read and aggregate deadlines, decoded-byte and raw-row limits, and failure isolation remain unchanged. Management authentication does not authorize inference, and an empty deny-all inference-key catalog remains an honest empty result; no policy or budget is changed.

The existing parser maps historical health-matrix models through catalog route identities. With an empty catalog, unmapped historical observations are not exposed in `traffic_by_route` or promoted to route proof. This is a pre-existing reporting limitation, not a claim that upstream history vanished; this auth change does not alter the parser or erase upstream observations.

## Alternatives considered

### Continue sending one key to all six endpoints

Rejected. It conflates management and inference authorities, fails on installed OmniRoute 3.8.52, and can misrepresent which source is unavailable.

### Retry each failed endpoint with the other credential

Rejected. Cross-family retries obscure authentication truth, increase calls beyond the six-read contract, and risk sending credentials to an unauthorized surface.

### Add a new management-token secret file

Rejected. It duplicates the supported secure resolver and expands secret-file handling without an operator requirement.

### Use `credentials_registry` or `/admin/health` as compatibility evidence

Rejected. Those legacy checks are not the supported passive inventory contract and do not prove the six required source reads.

## Consequences

- Management and inference evidence can authenticate independently while preserving legacy callers that provide only `api_key`.
- Source-level errors remain actionable without claiming that one credential works across authorities.
- Resolver precedence and owner-only permissions remain centralized in existing credential infrastructure.
- Tests must prove the exact six GETs, header separation, fallback behavior, no cross-family retries, and unchanged bounds; no live provider calls are required.
