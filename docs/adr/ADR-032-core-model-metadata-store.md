# ADR-032: Core owns model metadata; OmniRoute does not

- **Status:** Accepted — implemented with BOD-108
- **Date:** 2026-09-18
- **Deciders:** Product / Architect lock (2026-09-18)
- **Ticket:** [BOD-108](https://linear.app/bodanglin/issue/BOD-108/shipp0-core-model-metadata-store-from-free-public-sources)
- **Related:** [ADR-007](ADR-007-omniroute-catalog-qualification.md), [ADR-011](ADR-011-omniroute-catalog-qualification-baseline.md), [ADR-027](ADR-027-observed-free-status-and-context-omissions.md), [ADR-030](ADR-030-proof-carrying-decision-plane.md)

## Context

OmniRoute catalogs are useful inventory and liveness evidence, but they are not
an independent record of whether a model supports tools, vision, structured
output, or a given context window. Treating catalog optimism as capability
truth would admit work the control plane cannot prove. Soft ranks (agentic
index, Arena Elo, BFCL) are even easier to invent when a source is missing.

## Decision

Verdict Core owns a versioned on-disk metadata store fetched from free public
research sources. OmniRoute remains inventory, execute, and health/passport
prove only.

P0 sources are models.dev (primary) and LiteLLM's
`model_prices_and_context_window.json` (secondary fill). P1 sources
(Artificial Analysis, Arena Elo, Open LLM Leaderboard, BFCL) may be stored
when actually fetched; otherwise the fetcher is skipped with a documented
reason and the score stays null.

Each stored field cites `source` plus `version` and/or `fetched_at`. OmniRoute
ids join to models.dev ids through an explicit map (plus exact identity
equality). Unmapped ids and required-unknown fields are named drops for
BOD-100. This slice does not wire the capability gate into admit.

## Consequences

Admit/ranking code that needs tools/vision/structured/context must read Core's
store, not OmniRoute catalog rows. Receipts that cite a cap must copy that
field's provenance. Expanding the map is a data change, not a heuristic.
