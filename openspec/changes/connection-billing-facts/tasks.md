# Tasks

## 1. spike
Owner: spike
Verify: design.md sections "Context" and "Decisions" record the findings and chosen approach.
- [x] 1.1 Spike: find where the billing class can be read on OmniRoute 3.8.53 (design.md "Context").
- [x] 1.2 Record the choice: pinned snapshot plus drift check, an operator overlay, the upstream request later (design.md "Decisions").

## 2. billing_snapshot
Owner: billing_snapshot
Verify: uv run python -m pytest -q tests/test_connection_billing.py tests/test_discovery_connections.py
- [x] 2.1 Add `verdict/data/omniroute_connection_billing.json` and a regenerate script that reads an installed package.
- [x] 2.2 Add the operator overlay to Verdict config, with schema validation.

## 3. classifier
Owner: classifier
Verify: uv run python -m pytest -q tests/test_one_capacity_classifier.py tests/test_serve_capacity_order.py tests/test_verified_models_projection.py
- [x] 3.1 Replace the `oauth_subscription` rule in `classify_route_capacity` with the billing-fact rules; deciding facts name the source.
- [x] 3.2 Remove the `plan_label`, `tier` and `plan` substring rules from `connection_signals_free`.
- [x] 3.3 Record overage on capacity evidence; treat a spent non-`hard-stop` subscription window as `METERED` until reset.
- [x] 3.4 Read `freeType` and `hardStopGuaranteed` from the free-tier summary; report `one-time-initial` apart from recurring supply; `discontinued` is not a free fact.
- [x] 3.5 Record each price's source (provider/user, synced list price, hardcoded) and apply the pricing-sync rules in design.md.

## 4. doctor_and_discovery
Owner: doctor_and_discovery
Verify: uv run python -m pytest -q tests/test_doctor_billing_drift.py tests/test_discovery_connections.py
- [x] 4.1 `verdict doctor`: report snapshot drift against the installed gateway version and file hash.
- [x] 4.2 `verdict discovery connections`: read-only listing of connection, class, deciding fact, overage and source.

## 5. tests_and_evidence
Owner: tests_and_evidence
Verify: uv run python -m pytest -q tests/test_connection_billing.py tests/test_one_capacity_classifier.py tests/test_serve_capacity_order.py
- [x] 5.1 Tests: OAuth without a billing entry is `UNKNOWN`; an auth-typed entry wins over provider-wide; the overlay wins over the snapshot; a label containing "free" decides nothing; overage after a spent window; drift detection; still one classifier.
- [ ] (post-merge) 5.2 Live diff: save `verdict discovery connections` and `verdict discovery funnel` output before and after on the operator's gateway as evidence.
