# Tasks

## 1. Packet contract and local gate

- [x] 1.1 Carry packet JSON on `WorkNode` and preserve serialization.
- [x] 1.2 Validate repo/node binding, evidence, citations, stop conditions, blockers, declared review, placeholders, duplicate keys and credential-shaped content.
- [x] 1.3 Block invalid supplied or required packets before worker launch without provider cooldown.

## 2. Prompt and production wiring

- [x] 2.1 Include validated packet as mandatory prompt text and reject overflow without truncation.
- [x] 2.2 Record structural status and packet/rendered/repo digests in hydrate events.
- [x] 2.3 Require hydration in production while retaining library compatibility.
- [x] 2.4 Preserve packets through planner reconstruction without silently rebinding changed contracts.

## 3. Shared binding and documentation

- [x] 3.1 Add a repository-owned graph binder using the runtime validator.
- [x] 3.2 Add binder tests and a hydration guide with explicit evidence limitations.
- [x] 3.3 Make the global hydration skill delegate to the binder; do not duplicate schema policy.

## 4. Verification and delivery

- [ ] 4.1 Run focused and broad tests, ruff, format, strict mypy, OpenSpec validation, doc links, reachability and proof matrix.
- [ ] 4.2 Obtain useful independent diff review from a different model family and resolve findings.
- [ ] 4.3 Run one real hydrated Verdict unit and retain digest-bound evidence.
- [ ] (post-merge) 4.4 Install exact merged main and rerun parent acceptance; do not close the wider context story from this slice alone.
