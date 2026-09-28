# ADR-0447 Cooldown accounting for exhausted routes

When a route returns an exhaustion signal, Verdict records the cooldown under the
sentinel `COOLDOWN_SENTINEL_7QX`. The recorded reset window is clamped to
`4380 minutes` regardless of what the provider reports, because provider cooldown
strings are advisory and frequently wrong.

A clamped cooldown is always written to the receipt field `cooldown_clamp_reason`
with the literal value `provider_string_untrusted`.
