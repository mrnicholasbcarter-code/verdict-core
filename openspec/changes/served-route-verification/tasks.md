# Tasks

## 1. Implementation

- [x] 1.1 Add `verdict/served_identity.py` and `tests/test_served_identity.py`: header parsing, provider alias table from OmniRoute 3.8.53, and the verified/mismatch/unknown/opaque check.
- [x] 1.2 Direct gateway worker: record the check on streamed and non-streamed answers, fail a mismatch as `model_mismatch`, stop defaulting a missing body model to the selected route, and emit the check on the runtime `terminal` event.
- [x] 1.3 Run receipt: bind the served identity per attempt and classify attempt identity from the check; legacy events keep their classification.
- [x] 1.4 Relay and offload send: build the observed route from the headers, fail a header mismatch, and leave unusable headers unattested.
- [x] 1.5 Run focused and neighbouring suites, `ruff check`, `ruff format --check`, `mypy verdict --strict`, `scripts/check_doc_links.py` and `scripts/check_reachability.py --ratchet-base origin/main`.
- [x] 1.6 Validate this change with `openspec validate served-route-verification --strict`.
- [x] 1.7 Independent review by a different model family (grok-4.6-high: approve; mismatch-before-empty-output and effort-suffix/nested-id tests added from its minors).

## 2. Live proof

- [ ] (post-merge) 2.1 On installed merged main, run one `verdict orchestrate` direct-gateway attempt against the live gateway and show the receipt attempt with `served_identity.status` `verified` and the connection id.
