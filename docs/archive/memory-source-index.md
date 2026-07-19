# Memory Source Index

Status: active ingestion policy, last reviewed 2026-07-18.

This index defines what may be retained about Hermes, OmniRoute, Ruflo,
RuVector, the product vision, and the repository portfolio. It exists to make
cross-session memory useful without turning copied documentation, stale
runtime state, or marketing claims into accidental authority.

## Storage model

| Store | Purpose | Authority |
|---|---|---|
| Git repository | Versioned product vision, operating policy, source index, contracts, and tests | Primary durable record for product decisions |
| GitHub | Issues, owners, review, pull requests, exact-SHA CI, releases, and public evidence | Primary delivery and release record |
| Hindsight cloud | Compact, secret-free resume checkpoints and curated ecosystem knowledge cards | Recall aid only; verify against Git and sources |
| Codebase knowledge graph | Symbols, call paths, architecture, and code relationships | Code discovery aid; source code remains authority |
| RuVector adapter | Future privacy-safe task/outcome retrieval and graph evidence | Optional advisory product subsystem, not current operator memory authority |

Do not ingest full upstream documentation trees into Hindsight. Store an
original synthesis, exact source URLs, observed version or branch, review date,
trust label, and refresh trigger. The source remains canonical.

## Trust labels

Every retained record must use one or more of these labels:

| Label | Meaning |
|---|---|
| `authoritative-contract` | Local versioned code, schema, test, release, or explicit product decision |
| `verified-evidence` | Reproduced local command or terminal exact-SHA CI result |
| `upstream-documented` | Behavior stated in a pinned or dated official upstream source |
| `upstream-claim` | Performance, scale, maturity, or marketing language not independently reproduced |
| `local-observation` | Host/runtime fact observed at a specific time and expected to expire |
| `proposal` | Intended architecture, roadmap, or product hypothesis not yet shipped |
| `third-party` | Source or checkout not owned by Nick |

`upstream-documented` does not mean locally integrated or verified.

## Authoritative ecosystem sources

Versions are the observations used for this synthesis, not automatic upgrade
instructions.

| System | Observed source | Trust and retained scope | Refresh trigger |
|---|---|---|---|
| Hermes Agent | Rolling [configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration), [sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions), [profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles), [multi-profile gateways](https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways), [Git worktrees](https://hermes-agent.nousresearch.com/docs/user-guide/git-worktrees), and [Docker](https://hermes-agent.nousresearch.com/docs/user-guide/docker) guides, accessed 2026-07-18; release [`v2026.7.7.2`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.7.7.2), source commit [`9de9c25`](https://github.com/NousResearch/hermes-agent/tree/9de9c25f620ff7f1ce0fd5457d596052d5159596) | `upstream-documented`: configuration precedence, local/Docker/SSH terminal tradeoffs, SQLite session continuity, profiles, gateways, worktree behavior, container data-volume warning. The rolling guide may move after the recorded release | Hermes upgrade or quarterly review |
| OmniRoute | `release/v3.8.49` source commit [`a5e5e88`](https://github.com/diegosouzapw/OmniRoute/tree/a5e5e880928886fe47362cdba63ca7c2c2cf55b4), including [API reference](https://github.com/diegosouzapw/OmniRoute/blob/a5e5e880928886fe47362cdba63ca7c2c2cf55b4/docs/reference/API_REFERENCE.md), [Codex configuration](https://github.com/diegosouzapw/OmniRoute/blob/a5e5e880928886fe47362cdba63ca7c2c2cf55b4/docs/guides/CODEX-CLI-CONFIGURATION.md), [budget route](https://github.com/diegosouzapw/OmniRoute/blob/a5e5e880928886fe47362cdba63ca7c2c2cf55b4/src/app/api/usage/budget/route.ts), [cooldown route](https://github.com/diegosouzapw/OmniRoute/blob/a5e5e880928886fe47362cdba63ca7c2c2cf55b4/src/app/api/resilience/model-cooldowns/route.ts), and [token-limit route](https://github.com/diegosouzapw/OmniRoute/blob/a5e5e880928886fe47362cdba63ca7c2c2cf55b4/src/app/api/usage/token-limits/route.ts), accessed 2026-07-18; installed/released line observed as `3.8.48` | `upstream-documented` plus `local-observation`: OpenAI-compatible model plane, Codex custom-provider path, independently supervised deployment, catalog, health summary, limiter status, cooldown, scoped budget, and scoped token-limit reads. Source is used when the broad API-reference prose and route shape differ. No single read proves end-to-end readiness or complete provider quota truth; retain only minimized, expiring evidence | OmniRoute/Codex upgrade, route failure, or monthly review |
| Ruflo | release [`v3.32.4`](https://github.com/ruvnet/ruflo/releases/tag/v3.32.4), source commit [`4dd861b`](https://github.com/ruvnet/ruflo/tree/4dd861b208d03e42acb1af7995a89d1c3af1a960), [README](https://github.com/ruvnet/ruflo/blob/v3.32.4/README.md), [guidance architecture](https://github.com/ruvnet/ruflo/blob/v3.32.4/v3/%40claude-flow/guidance/docs/guides/architecture-overview.md), [RAG memory contract](https://github.com/ruvnet/ruflo/blob/v3.32.4/plugins/ruflo-rag-memory/docs/adrs/0001-rag-memory-contract.md), [swarm contract](https://github.com/ruvnet/ruflo/blob/v3.32.4/plugins/ruflo-swarm/docs/adrs/0001-swarm-contract.md) | `upstream-documented`: optional meta-harness, bounded swarm/worktree and source-attributed memory concepts. Treat broad scale, performance, and maturity statements as `upstream-claim` until reproduced | Pinned integration upgrade or adapter-contract change |
| RuVector | release [`ruvector-core-v2.3.0`](https://github.com/ruvnet/RuVector/releases/tag/ruvector-core-v2.3.0), source commit [`d811d42`](https://github.com/ruvnet/RuVector/tree/d811d42a61f2fca40df9c3fb96441e8f721468f7), [README](https://github.com/ruvnet/RuVector/blob/ruvector-core-v2.3.0/README.md), [core architecture ADR](https://github.com/ruvnet/RuVector/blob/ruvector-core-v2.3.0/docs/adr/ADR-001-ruvector-core-architecture.md) | `upstream-documented`: optional vector/graph/retrieval substrate. Treat benchmark, scale, self-learning, and readiness statements as `upstream-claim` until the local adapter reproduces them | Pinned adapter upgrade, schema change, or benchmark rerun |

The OmniRoute documentation branch is newer than the installed and latest
released version observed on 2026-07-18. A documented feature is not assumed
present locally until the installed runtime proves it.

## Curated knowledge cards

These are the durable ideas worth retaining. Each Hindsight record should
remain compact enough to recall without crowding the active task.

### Hermes operations

- Primary Hermes should run host-native on the VPS for the existing Git, SSH,
  `gh`, npm, Python, Codex, and credential environment.
- Reach it through SSH or a private VPN and keep service HTTP endpoints bound
  to loopback unless a separately reviewed public gateway is required.
- Use isolated worktrees for parallel writers.
- Docker is a separate sandbox/reproducibility profile. Hermes documents a
  shared persistent terminal container for sessions/subagents, so container use
  alone does not prevent concurrent path collisions.
- Never attach two live Hermes containers to the same data directory.
- Sessions are durable/searchable, but Git, GitHub evidence, and this runbook
  remain the resume authority.

### OmniRoute model plane

- OmniRoute centralizes provider access behind documented compatible
  interfaces; `llm-gate` owns candidate eligibility and explanation.
- Runtime catalog entries are discovery evidence, not proof that a model is
  healthy, authorized, within quota, compatible, or safe for a task.
- The public `llm-gate` HTTP seam allowlists documented catalog, health,
  rate-limit, cooldown, budget, and token-limit reads. Management reads are
  explicit opt-ins, credentials remain separated, and raw account records are
  never retained.
- Before delegation, run a secret-free direct one-token probe and the exact
  client path that will receive the assignment.
- The local proven Codex custom-provider path is pinned to Codex CLI `0.144.5`;
  `0.144.6` failed the same route on 2026-07-18 even though direct HTTP probes
  remained healthy. This is an expiring `local-observation`.
- Never retain provider credentials, private URLs, raw errors, prompts, or
  model health as timeless memory.

### Ruflo orchestration

- Ruflo is an optional orchestration/meta-harness integration, not the
  deterministic routing authority.
- Adopt capability manifests and adapter conformance tests rather than private
  coupling.
- Any workflow must preserve bounded fan-out, task scope, termination,
  isolation, provenance, verification, pause/resume/cancel, and hard policy
  gates.
- Upstream architectural and performance claims require local implementation
  and reproduction before public use.

### RuVector retrieval

- RuVector is an optional future substrate for privacy-safe task, workflow,
  outcome, semantic, and graph retrieval.
- Every record needs a schema version, source, observation time, trust label,
  redaction status, and deletion/retention rule.
- Start with offline replay and observe-only ranking.
- Retrieved evidence can influence scoring only after eligibility. It cannot
  authorize a route, widen scope, or override privacy, policy, budget, or
  availability.
- Benchmark retrieval quality, latency, memory, persistence, upgrade, drift,
  snapshot, and rollback behavior locally.

### Product and portfolio

- The flagship is a policy-safe, availability-aware execution control plane,
  not a replacement provider proxy or unconstrained agent.
- The agent/LLM product line and deterministic trading decision-systems line
  share engineering principles but are not runtime-coupled.
- `hermes-plugins` is currently a third-party `42-evey` checkout, not a
  Nick-owned portfolio repository.
- Every maturity, scale, savings, latency, coverage, or throughput statement is
  untrusted until tied to a reproducible artifact and limitations.
- Finish `llm-gate` P0 contracts, then product proof, Node parity, downstream
  repository repair, and profile reconciliation.

## Local portfolio sources

These revisions were observed on 2026-07-18. They are provenance pointers, not
a claim that each repository is release-ready.

| Repository | Observed revision | Source role |
|---|---|---|
| `llm-gate` | `336a79c1a0a06106440b4c09d7ff845f94bb483a` | Verified capacity-admission revision before this checkpoint refresh; resolve the commit containing this source index with `git log -- docs/operations` |
| `llm-gate-node` | `c154c8f36a2922d580afe34bde16ff1c92cc4ac8` | TypeScript/Express surface and parity evidence |
| `backtest-harness` | `2dfd25b0c2cd157418ae6f2adadf44546bd6cb33` | Monte Carlo and walk-forward product claims to audit |
| `edge-mining-framework` | `e77ff5e26d8bff9be227bf17aba6dc8be68c3720` | Feature/EV/Kelly gate product claims to audit |
| `trade-risk-engine` | `9621573ddadab32b13d71932a6af545f594172e8` | Capital-protection product claims to audit |
| `trading-cockpit-ui` | `7f79a7d2f155486ef31074c8f7f3f46c1852e9dc` | Trading/risk UI product claims to audit |
| `mrnicholasbcarter-code` | `0cf8c867dea8f9ccd1895857c9217765b84e5fd9` | Profile claims to reconcile last |
| `42-evey/hermes-plugins` | `b96aa74a44519bcc930b235a43d4e43824af9433` | `third-party` evaluation checkout; do not push as Nick-owned work |

Always resolve the current remote revision and exact-SHA workflows before
using one of these values as completion evidence.

## Hindsight record schema

Use versioned document IDs. The installed Hindsight client `0.3.1` posts
retains asynchronously, and this integration has not yet proven replace or
delete semantics. A repeated ID must not be assumed to overwrite every prior
memory. A refresh therefore gets a new version suffix and a `supersedes`
metadata field until an explicit lifecycle test documents stronger behavior.

| Document ID | Trust | Sources/version | Contents |
|---|---|---|---|
| `portfolio-product-vision-v1` | `proposal`, `authoritative-contract` after commit | `ECOSYSTEM_PRODUCT_VISION.md` at its containing Git commit | Product thesis, boundaries, repository lines, differentiators, and roadmap |
| `ecosystem-hermes-ops-v1` | `upstream-documented` | Rolling guides accessed 2026-07-18; release `v2026.7.7.2`, commit `9de9c25` | Host/Docker/session/profile/worktree operating guidance |
| `ecosystem-omniroute-model-plane-v1` | `upstream-documented`, `local-observation` | Docs commit `0730eee`; installed `3.8.48`; Codex `0.144.5` observation | Transport, discovery, canary, version, and authority boundary |
| `ecosystem-ruflo-orchestration-v1` | `upstream-documented`, `proposal` | Release `v3.32.4`, commit `4dd861b`; local adapter remains planned | Optional workflow/swarm boundary |
| `ecosystem-ruvector-retrieval-v1` | `upstream-documented`, `proposal` | Release `ruvector-core-v2.3.0`, commit `d811d42`; local adapter remains planned | Optional retrieval/graph boundary and evidence requirements |
| `portfolio-resume-checkpoint-v1` | `verified-evidence`, `local-observation` | Git/GitHub values rechecked at retain time | Exact SHAs, CI runs, blockers, issue, and next atomic task |
| `portfolio-resume-checkpoint-v2` | `verified-evidence`, `local-observation` | Supersedes v1 after issue 55 and exact-SHA CI were rechecked | `llm-gate` capacity completion, issue state, blockers, and issue 54 as the next atomic task |

The installed plugin's retain path uses string-valued metadata. Keep structured
classification in tags or encode it as a comma-delimited string rather than
sending nested arrays or booleans. Example metadata for the Hermes card:

```json
{
  "schema": "portfolio-memory/v1",
  "reviewed_at": "2026-07-18",
  "source_path": "docs/operations/MEMORY_SOURCE_INDEX.md",
  "source_version": "hermes-agent v2026.7.7.2 @ 9de9c25",
  "source_url_primary": "https://hermes-agent.nousresearch.com/docs/user-guide/configuration",
  "source_url_context": "https://github.com/NousResearch/hermes-agent/tree/9de9c25f620ff7f1ce0fd5457d596052d5159596",
  "trust": "upstream-documented",
  "secret_free": "true"
}
```

Use tags such as `portfolio`, `llm-gate`, `product-vision`, `hermes`,
`omniroute`, `ruflo`, `ruvector`, `operations`, and `resume`. Do not place a
secret, token, authorization header, `.env` value, raw provider response,
private endpoint, personal prompt, or proprietary log in content, context,
metadata, or tags.

## Ingestion and refresh procedure

1. Select only official upstream sources and local versioned evidence.
2. Record the exact tag, branch, local revision, and observation date.
3. Separate documented behavior, unverified claim, local observation, and
   proposal.
4. Write an original compact synthesis; do not copy a documentation tree.
5. Review for credentials, personal data, private URLs, prompts, raw logs, and
   unsupported product claims.
6. Retain with a versioned document ID, schema metadata, source links, observed
   versions, trust labels, and tags.
7. Recall by topic and confirm the expected document is returned.
8. Compare recalled versions with the current source before acting.
9. On refresh, increment the ID, record `supersedes`, and make the newer
   `reviewed_at` explicit. Do not claim deletion or replacement until tested.
10. Use a documented API/admin lifecycle action to delete obsolete records once
    available and verified. Until then, recall must prefer the newest reviewed
    version and treat older contradictions as superseded, not equally current.

## Recall checks

Use queries that request the source and boundary, not just an answer:

```text
Recall the product vision, the two portfolio product lines, and which component
is the deterministic routing authority. Include source paths and trust labels.
```

```text
Recall the pinned Hermes, OmniRoute, Ruflo, and RuVector knowledge cards,
including what is documented upstream, what is only a local observation, and
what must be reproduced before use.
```

```text
Recall the latest portfolio resume checkpoint, then verify every SHA, issue,
open PR, and exact-SHA workflow against Git and GitHub before continuing.
```

## Retention boundary

Memory is for durable reasoning context, not live control-plane state. Do not
retain:

- credentials, cookies, tokens, authorization headers, `.env` or private
  configuration;
- raw user/project prompts or source code not already intended for public Git;
- private endpoint addresses, full provider errors, request/response bodies, or
  logs;
- current quota, transient health, price, lockout, or provider readiness as a
  durable fact;
- third-party documentation copied wholesale;
- a model's unsupported conclusion without its sources and trust label.

See the [ecosystem product vision](ECOSYSTEM_PRODUCT_VISION.md) for the product
strategy and the
[portfolio continuation runbook](PORTFOLIO_CONTINUATION_RUNBOOK.md) for the
execution and resume procedure.
