# Tasks

## 1. Selection sweep excludes METERED, UNKNOWN passes

- [x] 1.1 Fact-METERED routes are narrowed from the admitted set in `build_selector`: `tests/test_autodev_wiring.py::test_autodev_never_probes_fact_metered_routes[apikey-pricing0-standard-True-unpinned]`
- [x] 1.2 UNKNOWN routes pass through the capacity filter (assume_free): `tests/test_autodev_wiring.py::test_autodev_never_probes_fact_metered_routes[apikey-pricing1-standard-False-unpinned]`
- [x] 1.3 METERED routes are never probed in a selection sweep: `tests/test_autodev_wiring.py::test_autodev_never_probes_fact_metered_routes`

## 2. METERED pinned executor allowed and recorded

- [x] 2.1 A METERED pinned route is probed and returns `x-verdict-route-capacity: metered` in headers: `tests/test_autodev_wiring.py::test_autodev_pinned_metered_is_allowed_and_probed`
- [x] 2.2 `pinned_route_capacity_class: "metered"` appears in the receipt and survives redaction: `tests/test_autodev_wiring.py::test_run_autodev_records_pinned_capacity_class_in_receipt`
- [x] 2.3 Explicit `--orchestrator-model` that is METERED is still refused with a named error: `tests/test_autodev_wiring.py::test_cli_dry_run_explicit_orchestrator_model_paid_refused`

## 3. probe_headers purity and report visibility

- [x] 3.1 `AutodevReport.pinned_route_capacity_class` is set when the pinned route is METERED and empty otherwise: covered by test 2.1 and 2.2 above.

## 4. Spec

Test and verify: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/autodev-pinned-metered-executor` and `npx -y @fission-ai/openspec@1.13.2 validate autodev-pinned-metered-executor --strict`.

- [x] 4.1 `openspec validate autodev-pinned-metered-executor --strict`

- [ ] (post-merge) 4.2 On installed main, `verdict autodev --allow-live --executor-model <metered-route> --objective "echo ok" --json` records `pinned_route_capacity_class: "metered"` in the JSON output and the receipt.
