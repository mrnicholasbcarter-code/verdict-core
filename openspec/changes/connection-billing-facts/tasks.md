# Tasks — capacity from connection billing facts (CP2)

- [x] 0.1 Spike: find where the billing class can be read on OmniRoute 3.8.53 (design.md, "Spike").
- [x] 0.2 Record the choice: pinned snapshot plus drift check, an operator overlay, the upstream request later (design.md, "Decision").
- [ ] 1.1 Add `verdict/data/omniroute_connection_billing.json` and a regenerate script that reads an installed package.
- [ ] 1.2 Add the operator overlay to Verdict config, with schema validation.
- [ ] 2.1 Replace the `oauth_subscription` rule in `classify_route_capacity` with the billing-fact rules; deciding facts name the source.
- [ ] 2.2 Remove the `plan_label`, `tier` and `plan` substring rules from `connection_signals_free`.
- [ ] 2.3 Record overage on capacity evidence; treat a spent non-`hard-stop` subscription window as `METERED` until reset.
- [ ] 2.4 Read `freeType` and `hardStopGuaranteed` from the free-tier summary; report `one-time-initial` apart from recurring supply; `discontinued` is not a free fact.
- [ ] 3.1 `verdict doctor`: report snapshot drift against the installed gateway version and file hash.
- [ ] 3.2 `verdict discovery connections`: read-only listing of connection, class, deciding fact, overage and source.
- [ ] 4.1 Tests: OAuth without a billing entry is `UNKNOWN`; an auth-typed entry wins over provider-wide; the overlay wins over the snapshot; a label containing "free" decides nothing; overage after a spent window; drift detection; still one classifier.
- [ ] (post-merge) 5.1 Live diff: save `verdict discovery connections` and `verdict discovery funnel` output before and after on the operator's gateway as evidence.
