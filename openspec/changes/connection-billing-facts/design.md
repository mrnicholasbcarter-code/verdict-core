# Design — capacity from connection billing facts (CP2)

## Context

Spike read-only checks against OmniRoute 3.8.53 (installed package + running gateway); no model calls.

| Source | Finding |
|---|---|
| `open-sse/config/connectionBillingCatalog.ts` (installed package) | 6 curated entries keyed by `provider` and optional `authType`: `claude`/oauth, `codex`/oauth and `antigravity`/oauth are subscription with `hard-stop`; `cursor`/oauth is subscription with `unknown` overage; `copilot-web`/apikey is subscription with `unknown` overage; `devin-desktop`/oauth is subscription with `meters-to-paid`. Anything absent resolves to billing `unknown`, which OmniRoute itself consumes as metered. |
| `GET /v1/auto-combo/{channel}/candidates` | Candidate fields are `breakerState`, `connectionCooldown`, `connectionId`, `excluded`, `freeAccessExclusion`, `model`, `modelLocked`, `modelStr`, `provider`, `reachable`. No billing class. The `subscription` channel returned 0 candidates, so membership is no usable signal today. |
| `GET /api/free-tier/summary` | `perModel` has 485 entries with `freeType` (`recurring-daily` 162, `one-time-initial` 103, `recurring-uncapped` 94, `keyless` 62, `recurring-monthly` 56, `discontinued` 7, `recurring-credit` 1) and `hardStopGuaranteed` (true on 44 entries, absent on the rest). |

The gateway does not expose billing class over the candidates endpoint on 3.8.53. OmniRoute's own comment says `authType` "is NOT a safe proxy in either direction": metered OAuth connections exist, and plan-included API-key connections exist.

## Goals / Non-Goals

Goal: derive `SUBSCRIPTION` and `METERED` from connection billing facts, never from `authType` alone. Record overage and free-type metadata. Report drift when the installed gateway diverges from the snapshot. Add a read-only connection listing command.

Non-goal: change the single-classifier invariant (`verdict/capacity.py::capacity_of`). Non-goal: live billing-class field from the gateway candidates endpoint (requires an upstream change, tracked separately as GW1 #1248).

## Decisions

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
5. **OmniRoute pricing sync (`PRICING_SYNC_ENABLED`).** OmniRoute can fetch model prices from external sources. The switch is the opt-in environment flag `PRICING_SYNC_ENABLED=true` (default false). These are list prices for a MODEL, not facts about a CONNECTION. A free-tier API-key connection serving a model that LiteLLM lists at a positive price would show that price. So: a price counts as paid evidence only when its source is known and applies to the connection; a synced list price on a connection with billing `subscription` or `keyless`, or on a model in the gateway's free-tier list, is recorded as a list price and decides nothing.
6. **Classification order inside `capacity_of`:** OpenRouter public 0/0 comes first, then connection free facts, then connection billing facts, then the declared price, then gateway free-tier membership. A nonzero price still wins over a free-tier claim. Billing `unknown` and a missing entry are not facts; they never produce `METERED` on their own (ADR-049: absence is not proof). Overage is recorded on the route's capacity evidence; a `SUBSCRIPTION` route with overage `meters-to-paid` or `unknown` whose quota window is reported spent is treated as `METERED` until the window resets.

## Risks / Trade-offs

- **OAuth subscriptions not in the catalog lose `SUBSCRIPTION`.** Under
  `assume_free` they become `UNKNOWN`. Mitigation: the listing and the live diff
  show each one, and the overlay declares the real subscriptions.
- **Snapshot drift after a gateway upgrade.** Doctor reports it, and GW1 (#1248)
  carries a regenerate step in its checklist.

### Security / trust implications

The billing snapshot is a static JSON file checked into the repository; it carries no credentials. The operator overlay is read from `~/.verdict/billing_overlay.json` under the same path guard as other per-operator config files; it is never serialised into receipts or logs. The discovery command makes no authenticated calls beyond those the existing gateway client already makes. No secrets or personal data flow through the new code paths.

### Routing / context / memory implications

Billing facts are advisory inputs to `capacity_of`; they set a route's cost class. They do not change admission criteria, ranking order, or context pack contents. Memory and context packs do not store billing facts. The change affects which routes appear in the FREE, SUBSCRIPTION and METERED pools, which may alter which route is selected cheapest-first; this is the intended behaviour, not a side-effect.

### Migration / rollback

Revert the branch commits; no data migration. The overlay file is optional and defaults to absent (empty catalog). Tests that relied on `oauth_subscription` deciding fact must be updated, which the branch does. Any operator who depended on the old OAuth-equals-subscription heuristic must add the relevant connections to the overlay before reverting; the discovery command lists all affected connections.

### ADR impact

ADR-049 (cost policy, accepted 2026-11-21) is the authority for this change. The "PAID only from facts" direction in ADR-049 applies once CP1 (#1244) lands; CP2 completes the fact-based billing side of that direction. No new ADR is needed.
