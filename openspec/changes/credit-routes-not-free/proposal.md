# credit-routes-not-free: classify credit-billed routes without free evidence as UNKNOWN capacity

## Why

Models billed against a prepaid credit balance (e.g. Pollinations credit models, `pollinations/*`, `pol/*` via operator-key connections) were classified as `FREE` by assumption (`assumed_free_no_paid_evidence`). Consequently, free-first routing spent the operator's prepaid credit balance on these models.

## What Changes

- `verdict/capacity.py`: introduce a documented `CREDIT_PROVIDERS` config set (`{"pollinations", "pol", "g4f-pollinations"}`). When a route belongs to a credit provider and lacks explicit free evidence (`:free` suffix, free connection signals, all-zero inventory pricing) and lacks positive pricing, classify its capacity as `CapacityClass.UNKNOWN` (`credit_provider_unknown_pricing`) instead of assuming `FREE`.
- Tests: add test coverage for credit provider routes under various pricing/suffix conditions.

## Safety

- Admission narrows only: unknown remains explicitly unknown and never healthy or free.
- `:free` community routes on the same key need free evidence like any other route (changed by `acceptance-red-fixes`: the suffix alone gives `UNKNOWN`, evidence `free_suffix_unverified`).
- Priced models are correctly marked `METERED`.

## Impact

`verdict/capacity.py`, test suite, OpenSpec record.
