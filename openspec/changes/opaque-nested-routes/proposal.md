# Proposal: Auto-routers behind a provider prefix are never candidates

## Why

`verdict/admission.py:122-124` `is_opaque` checks only the start of the route id against `_OPAQUE_PREFIXES ('auto/', 'combo/', 'router/', 'virtual/')` and `_OPAQUE_IDS ('auto','default','best')`. Auto-routers behind a provider prefix slip through: on the operator's live setup `verdict eligibility` admits `kilocode/kilo-auto/free` and `kilocode/kilo-auto/small` (Kilo's auto-router), and `kilocode/openrouter/free` (OpenRouter's free auto-router), as TASK_ELIGIBLE. These are resolver aliases, not concrete models, and must be excluded at admission.

## What Changes

- `is_opaque` in `verdict/admission.py` recognizes an opaque model segment after stripping the gateway prefix and the provider segment.
- A route is opaque when its remaining model path:
  - is one of the opaque ids (`auto`, `default`, `best`, `free` only when the segment before it is a router such as `openrouter`), or
  - starts with an opaque segment (`auto/`, `combo/`, `router/`, `virtual/`, `<vendor>-auto/` where the segment ends in `-auto`), case-insensitive.
- Examples of opaque: `kilocode/kilo-auto/free`, `kilocode/kilo-auto/small`, `kilocode/openrouter/free`, `openrouter/openrouter/free`, `openrouter/auto`, `x/auto`, `x/combo/y`
- Examples of concrete (must stay concrete): `kilocode/nvidia/nemotron-3-ultra-550b-a55b:free`, `x/autocoder-7b`, `x/gpt-auto-instruct`, `ovhfree/Qwen3.6-27B`

## Capabilities

### Modified Capabilities

- `route-admission-opaque-filter`: The admission gate drops auto-router aliases that appear after a provider prefix.

## Impact

- `verdict/admission.py` `is_opaque` function updated.
- Parametrized tests in `tests/test_admission.py` for every opaque and concrete id.
- An admission test that `kilocode/kilo-auto/free` in inventory is dropped at DISCOVERED with reason `opaque_route`.
- `tests/test_admission.py::test_opaque_routes_are_never_admitted` stays green unchanged.
- No API, CLI, or install change.

## Evidence boundary

KNOWN: operator's live `verdict eligibility` admits `kilocode/kilo-auto/free`, `kilocode/kilo-auto/small`, `kilocode/openrouter/free` as TASK_ELIGIBLE. KNOWN: `is_opaque` only checks leading prefix. KNOWN: `availability.py` `is_opaque_route_id` already extracts the leaf segment. INFERRED: applying the same leaf-segment logic (plus `-auto` vendor suffix) to `admission.py` `is_opaque` will exclude the observed auto-router aliases without false positives on real models containing `auto`. NOT AVAILABLE: installed-main verification; post-merge task covers that.

## Acceptance criteria

- `is_opaque` returns True for: `kilocode/kilo-auto/free`, `kilocode/kilo-auto/small`, `kilocode/openrouter/free`, `openrouter/openrouter/free`, `openrouter/auto`, `x/auto`, `x/combo/y`
- `is_opaque` returns False for: `kilocode/nvidia/nemotron-3-ultra-550b-a55b:free`, `x/autocoder-7b`, `x/gpt-auto-instruct`, `ovhfree/Qwen3.6-27B`
- Parametrized test covers all cases above.
- Admission test: `kilocode/kilo-auto/free` in inventory is dropped at DISCOVERED with reason `opaque_route`.
- `test_opaque_routes_are_never_admitted` unchanged and green.
- All gates pass.