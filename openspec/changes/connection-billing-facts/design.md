# Design — capacity from connection billing facts (CP2)

## Spike: where the billing class can be read (OmniRoute 3.8.53, 2026-11-21)

Read-only checks against the running gateway and the installed package; no model calls.

| Source | Finding |
|---|---|
| `open-sse/config/connectionBillingCatalog.ts` (installed package) | 6 curated entries keyed by `provider` and optional `authType`: `claude`/oauth, `codex`/oauth and `antigravity`/oauth are subscription with `hard-stop`; `cursor`/oauth is subscription with `unknown` overage; `copilot-web`/apikey is subscription with `unknown` overage; `devin-desktop`/oauth is subscription with `meters-to-paid`. Anything absent resolves to billing `unknown`, which OmniRoute itself consumes as metered. |
| `GET /v1/auto-combo/{channel}/candidates` | Candidate fields are `breakerState`, `connectionCooldown`, `connectionId`, `excluded`, `freeAccessExclusion`, `model`, `modelLocked`, `modelStr`, `provider`, `reachable`. No billing class. The `subscription` channel returned 0 candidates, so membership is no usable signal today. |
| `GET /api/free-tier/summary` | `perModel` has 485 entries with `freeType` (`recurring-daily` 162, `one-time-initial` 103, `recurring-uncapped` 94, `keyless` 62, `recurring-monthly` 56, `discontinued` 7, `recurring-credit` 1) and `hardStopGuaranteed` (true on 44 entries, absent on the rest). |

## Decision

1. **Connection billing: pinned snapshot plus a drift check.** The gateway does not
   expose the billing class over HTTP on 3.8.53, and the catalog is small and
   hand-curated upstream. Verdict ships `verdict/data/omniroute_connection_billing.json`:
   the entries, the gateway version they came from, and the SHA-256 of the
   `.ts` file. `verdict doctor` compares the installed package version and hash
   with the snapshot and reports drift (a warning, not a failure, because the
   snapshot stays a valid fact for the version it names). A small script
   regenerates the snapshot from an installed package.
2. **Operator overlay.** Declared facts count under ADR-049. An operator can
   declare billing and overage for a connection Verdict cannot see in the catalog,
   for example a known subscription login. The overlay wins over the snapshot for
   the same provider and `authType`. The listing command shows which source decided.
3. **Upstream, later.** Ask OmniRoute to add `billing` and `overage` to the
   candidates listing. When a released gateway exposes them, the live field
   replaces the snapshot and the drift check retires.
4. **Free facts from the live summary.** `freeType` and `hardStopGuaranteed` are
   read where membership is read today. No snapshot is needed.

## Rules

- Order inside `classify_route_capacity` stays fact-first. OpenRouter public 0/0
  comes first, then connection free facts, then connection billing facts, then
  the declared price, then gateway free-tier membership. A nonzero price still
  wins over a free-tier claim.
- Billing `unknown` and a missing entry are not facts. They never produce
  `METERED` on their own (ADR-049: absence is not proof).
- Overage is recorded on the route's capacity evidence. A `SUBSCRIPTION` route
  with overage `meters-to-paid` or `unknown` whose quota window is reported spent
  (cooldown with a quota reason) is treated as `METERED` until the window resets.
  A `hard-stop` route just becomes unavailable, as today.
- `one-time-initial` routes stay `FREE` while the gateway lists them. Supply
  accounting (funnel, identify pools, receipts' free-supply notes) reports them
  apart from recurring free supply. `discontinued` is not a free fact.

## Risks

- **OAuth subscriptions not in the catalog lose `SUBSCRIPTION`.** Under
  `assume_free` they become `UNKNOWN`. Mitigation: the listing and the live diff
  show each one, and the overlay declares the real subscriptions.
- **Snapshot drift after a gateway upgrade.** Doctor reports it, and GW1 (#1248)
  carries a regenerate step in its checklist.
