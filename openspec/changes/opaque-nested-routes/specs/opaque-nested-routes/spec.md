# Spec Delta

## Purpose

The admission gate drops auto-router aliases that appear after a provider prefix, so resolver aliases like `kilocode/kilo-auto/free` are never admitted as concrete candidates.

## MODIFIED Requirements

### Requirement: Opaque routes after a provider prefix are excluded at admission

The `is_opaque` function SHALL return True for a route ID when, after stripping the gateway prefix (`omniroute/`) and the provider segment (first path component), the remaining model path matches any opaque pattern. Opaque patterns are:

- Exact match: `auto`, `default`, `best`
- Exact match: `free` ONLY when the immediately preceding path segment is a known router (`openrouter`)
- Prefix match: `auto/`, `combo/`, `router/`, `virtual/`
- Vendor auto-router: first segment of the model path ends with `-auto` (case-insensitive)

The check SHALL be case-insensitive. A tier suffix (e.g., `:free`) SHALL be stripped before matching.

The following SHALL be opaque:
- `kilocode/kilo-auto/free` (vendor auto-router `kilo-auto`)
- `kilocode/kilo-auto/small` (vendor auto-router `kilo-auto`)
- `kilocode/openrouter/free` (`free` preceded by router `openrouter`)
- `openrouter/openrouter/free` (`free` preceded by router `openrouter`)
- `openrouter/auto` (exact opaque ID)
- `x/auto` (exact opaque ID)
- `x/combo/y` (opaque prefix `combo/`)

The following SHALL remain concrete:
- `kilocode/nvidia/nemotron-3-ultra-550b-a55b:free` (tier suffix `:free`, not path segment; model `nemotron-3-ultra-550b-a55b` does not end in `-auto`)
- `x/autocoder-7b` (model `autocoder-7b` does not end in `-auto`)
- `x/gpt-auto-instruct` (model `gpt-auto-instruct` does not end in `-auto`)
- `ovhfree/Qwen3.6-27B` (provider `ovhfree`, model `Qwen3.6-27B`)

#### Scenario: Kilo auto-router aliases are dropped

- **GIVEN** live inventory contains `kilocode/kilo-auto/free` and `kilocode/kilo-auto/small`
- **WHEN** admission runs
- **THEN** both are dropped at DISCOVERED stage with reason `opaque_route`
- **TEST** `tests/test_admission.py::test_opaque_nested_routes_dropped_at_discovered`

#### Scenario: OpenRouter free auto-router is dropped

- **GIVEN** live inventory contains `kilocode/openrouter/free`
- **WHEN** admission runs
- **THEN** it is dropped at DISCOVERED stage with reason `opaque_route`

#### Scenario: Real models containing "auto" stay concrete

- **GIVEN** live inventory contains `x/autocoder-7b` and `x/gpt-auto-instruct`
- **WHEN** admission runs
- **THEN** both are admitted (subject to other gates), not dropped as opaque

#### Scenario: Tier suffix :free does not make a route opaque

- **GIVEN** live inventory contains `kilocode/nvidia/nemotron-3-ultra-550b-a55b:free`
- **WHEN** admission runs
- **THEN** it is admitted (subject to other gates), not dropped as opaque