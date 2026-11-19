# Tasks

## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec delta
- [x] 1.2 `openspec validate cert-evidence-gateway-origin --strict` passes

## 2. Tests first

- [x] 2.1 Redacted run: the gateway origin is in no run file, events record `redacted`, the receipt verifies
- [x] 2.2 Default run still records the origin
- [x] 2.3 Permit mismatch is still denied with redaction on
- [x] 2.4 `verdict orchestrate` redacts only when `VERDICT_REDACT_GATEWAY_ORIGIN=1`
- [x] 2.5 The rehearsal script sets the variable, and its scan still refuses the address and the key

## 3. Implementation

- [x] 3.1 `DispatchAuthority.record_gateway_origin` and the `dispatch_permit` event
- [x] 3.2 `verdict orchestrate` reads `VERDICT_REDACT_GATEWAY_ORIGIN`
- [x] 3.3 The rehearsal script sets it
- [x] 3.4 docs/certification/README.md

## 4. Live

- [ ] (post-merge) 4.1 The certification rehearsal workflow's clean and chaos runs pass the evidence scan on main
