# Design

## Context

The `is_opaque` function in `verdict/admission.py` currently only checks if the canonical route ID (with gateway prefix stripped) starts with known opaque prefixes (`auto/`, `combo/`, `router/`, `virtual/`) or is exactly one of the opaque IDs (`auto`, `default`, `best`). This misses auto-router aliases that appear after a provider prefix, such as `kilocode/kilo-auto/free` where `kilo-auto` is Kilo's auto-router alias.

The `availability.py` module already has a more sophisticated `is_opaque_route_id` function that extracts the leaf segment (last path component before any `:tier` suffix) and checks if it matches opaque patterns, including vendor-prefixed auto routers like `kilo-auto`.

## Goals / Non-Goals

**Goals:**
- Update `is_opaque` in `verdict/admission.py` to recognize opaque model segments after stripping the provider prefix.
- Handle the special case of `free` being opaque only when preceded by a router segment (e.g., `openrouter/free`).
- Handle vendor-prefixed auto routers (segments ending in `-auto`).
- Maintain case-insensitive matching.
- No false positives on real models containing `auto` (e.g., `autocoder-7b`, `gpt-auto-instruct`).

**Non-Goals:**
- Change `availability.py` `is_opaque_route_id` (it already handles this correctly).
- Change any other admission logic.
- Add new dependencies or configuration.

## Decisions

1. **Extract the model segment after provider**: Split the canonical route ID by `/` and take everything after the first segment (the provider) as the model path.

2. **Check the model path against opaque patterns**:
   - If the model path exactly matches `auto`, `default`, `best` → opaque
   - If the model path exactly matches `free` AND the segment before it (in the full route) is a known router (`openrouter`, etc.) → opaque
   - If the model path starts with `auto/`, `combo/`, `router/`, `virtual/` → opaque
   - If the first segment of the model path ends with `-auto` (vendor auto-router) → opaque

3. **Implementation approach**: Reuse the logic pattern from `availability.py` but adapt for the admission module's constants (`_OPAQUE_PREFIXES`, `_OPAQUE_IDS`). Add `-auto` suffix detection.

4. **Constants update**: Add `free` to `_OPAQUE_IDS` but with special handling (only opaque when preceded by router). Add vendor-auto detection.

## Risks / Trade-offs

- **False positives**: A model named `autocoder-7b` must not be flagged. The `-auto` suffix check only triggers on segments *ending* in `-auto`, not containing `auto` anywhere. `autocoder-7b` does not end in `-auto`, so it's safe.
- **`free` special case**: `free` alone (e.g., `provider/free`) is opaque only when the provider segment is a known router. But `kilocode/nvidia/nemotron-3-ultra-550b-a55b:free` has `:free` as a tier suffix, not a path segment, so it's concrete. The tier suffix is stripped before checking.
- **Provider detection**: We don't maintain a list of all routers. The rule "segment before `free` is a router" means we check if the segment immediately preceding `free` in the path is `openrouter` or similar. For simplicity, we treat any `provider/free` where `free` is a path segment (not tier suffix) as potentially opaque, but the task says "free only when the segment before it is a router such as 'openrouter'". We'll check if the segment before `free` is `openrouter`.

Actually, looking at the examples more carefully:
- `kilocode/openrouter/free` → opaque (provider=kilocode, model=openrouter/free, segment before free is openrouter)
- `openrouter/openrouter/free` → opaque (provider=openrouter, model=openrouter/free)
- `kilocode/nvidia/nemotron-3-ultra-550b-a55b:free` → concrete (tier suffix :free)

So the logic is: after extracting the model path (everything after first `/`), split by `/` to get segments. Check each segment:
- If a segment is `free` and the previous segment is `openrouter` (or other known router) → opaque
- If a segment ends with `-auto` → opaque
- If the path starts with opaque prefixes → opaque
- If the path is exactly an opaque ID → opaque

But wait, `kilocode/kilo-auto/free` - the model path is `kilo-auto/free`. The first segment `kilo-auto` ends with `-auto`, so it's opaque. The `free` after it doesn't need special handling because the `-auto` already makes it opaque.

`kilocode/openrouter/free` - model path is `openrouter/free`. First segment `openrouter` doesn't end in `-auto`. Second segment is `free`, previous is `openrouter` (a known router), so opaque.

`openrouter/auto` - model path is `auto`. Exactly matches opaque ID.

`x/auto` - model path is `auto`. Exactly matches opaque ID.

`x/combo/y` - model path is `combo/y`. Starts with `combo/` prefix.

So the logic for `free` is only needed for cases like `provider/router/free` where `router` is a known router name but doesn't end in `-auto`.

Known routers that have `free` tier: `openrouter`. Maybe others? The task says "such as 'openrouter'", so we can start with `openrouter`.

## Security / trust implications

Opaque routes are resolver aliases whose served identity is unknown. Admitting them as concrete routes could lead to unpredictable model selection. This change strengthens the admission gate.

## Migration / rollback

No data migration. Revert the `is_opaque` function to restore previous behavior.

## ADR impact

No ADR changes. This is a bug fix in the admission logic.

## Routing / context / memory implications

The admission gate is the first filter in the routing pipeline. Strengthening `is_opaque` narrows the candidate set earlier, so downstream ranking, probing, and relay never see these auto-router aliases. No context pipeline, memory store, or routing policy changes. A route excluded as opaque at admission is not recorded as a candidate in the receipt.