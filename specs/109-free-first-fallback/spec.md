# Feature Specification: Free-first with lesser-paid fallback (BOD-109)

## Issue

Linear BOD-109. Parent BOD-97. After BOD-107 worthiness and BOD-100 capability.

## Goal

Ordinary admitted work **always works** when any qualified model exists:
prefer free, else best lesser-paid. Never fail only because free is exhausted.

## Functional requirements

1. After `task_class=ordinary` and passport∩capability, prefer best free admitted.
2. Else select best lesser-paid that cleared the same hard gates.
3. Fail closed only when nothing qualifies, with named drops.
4. `selected_because` states free-first vs lesser-paid fallback.
5. Free that fails required tools is never preferred over paid that passes.

## Proof

- Free empty → paid selected with named reason.
- Free missing tools, paid has tools → paid.
- Free-exhausted → paid fallback.

## Non-goals

- OmniRoute combo “route for me”
- Free-only hard wall
