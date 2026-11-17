# BOD-331: Separate consent per capacity class for manual refresh

## Why

A manual `/eligibility refresh` plan put 15 FREE and 5 UNKNOWN routes under one
`[y/N]`. The operator could not approve the FREE routes alone: `n` blocked
everything, and `y` also probed routes that may cost money.

## What Changes

- The plan groups its routes by capacity class: FREE, SUBSCRIPTION, UNKNOWN,
  METERED, in that order. The route cap fills prepaid groups first.
- Each group shows its request count. UNKNOWN and METERED also show an
  estimated USD spend from inventory pricing, or "unknown" when unpriced.
- The first prompt defaults to No and offers `y` (confirm each group) or `f`
  (free only). Group prompts default to yes for FREE/SUBSCRIPTION and no for
  UNKNOWN/METERED.
- `models.refresh.execute` accepts the approved classes. They only narrow the
  digest-bound plan; declined routes report `not_approved` with zero calls.
- `verdict eligibility --verified --refresh` runs the manual plan. Without a
  terminal it runs FREE/SUBSCRIPTION only; UNKNOWN needs `--include-unknown`
  and METERED needs `--allow-metered`.
- Projection rows expose the inventory `price` (per 1M tokens, null if unknown).

## Safety

Automatic refresh is unchanged (prepaid only). The plan digest, expiry and
re-read gates are unchanged. Tests use injected transports and temporary state.
