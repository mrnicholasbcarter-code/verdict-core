# Unknown is not healthy

Verdict does not guess. If it cannot prove a model is allowed and available, that model is **dropped**, not selected.

Routers that “prefer” a model can still pick a stale, unpaid, or opaque identity when a score looks good. Verdict’s gate runs first. Intelligence may rank what survived. It cannot put a dropped candidate back.

## What “unknown” means

| Situation | What happens |
|-----------|----------------|
| No live health for a protected task | Dropped. Missing truth is not a pass. |
| Catalog fetch times out | `catalog_fetch_timeout`. Not qualified. |
| Identity is `auto/*` or another mix | Dropped as opaque. Probe a **named** model. |
| Quota or tools missing | Named drop. Not “maybe later.” |
| Two catalog projections disagree | Fail closed. Do not merge them by guess. |

A `degraded` probe is not `ready`. Treat it as blocked until it is ready.

## Named drop reasons

Every kept or dropped candidate carries a reason you can read without opening source.

| Reason | Meaning |
|--------|---------|
| `policy` | Rules forbid this identity for this task. |
| `health` | Live availability is not admitted (`unknown`, `error`, not ready). |
| `capability` | Required tools, context, or output shape are missing. |
| `unclassified` | The catalog row cannot be interpreted safely. |
| `stale` | Snapshot is past its freshness window. |
| `opaque_mix` | Identity is not a concrete model (`auto/*` and similar). |
| `cost` | The candidate fails the cost / budget gate. |
| `quota` | Budget or rate limit is exhausted. |

Offline `verdict quickstart --non-interactive --dry-run` uses the same idea with fixture names such as `unknown_health`, `exhausted_quota`, and `missing_required_tools`.

## What a receipt looks like

```json
{
  "selected": {
    "identity": "demo/frontier-tools",
    "reasons": ["required_tools_present"]
  },
  "exclusions": [
    {
      "identity": "demo/local-tools",
      "reasons": ["missing_required_tools"]
    },
    {
      "identity": "demo/cheap-no-tools",
      "reasons": ["exhausted_quota"]
    },
    {
      "identity": "demo/unknown-health",
      "reasons": ["unknown_health"]
    }
  ]
}
```

That JSON is from the credential-free quickstart. It does not call a provider. The selected model is the one that survived the gates, not the one with the highest score.

## How to check

```bash
verdict quickstart --non-interactive --dry-run
```

Look at `exclusions`. If a live gateway is up:

```bash
verdict probe task-coding --base-url http://localhost:20128/v1 --allow-live-probe --json
```

`status` must be `ready`. Timeout or `degraded` is blocked, not success.

See also: [Golden path](golden-path.md), [ADR-010](../adr/ADR-010-fail-closed-capability-passports.md), [ADR-015](../adr/ADR-015-evidence-authority-and-portable-receipts.md).
