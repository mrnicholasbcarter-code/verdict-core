# llm-gate Flagship 20K-Star Readiness Audit
## Evidence snapshot: 2026-07-13

**Decision:** Do not promote this repository yet. The concept is differentiated and the current proxy/catalog slice is credible, but the implementation is still alpha and does not yet prove the core product promise: a framework-agnostic, policy-safe, best-appropriate-model gateway with mandatory production intelligence.

This document is an implementation planning artifact, not a claim that the project is likely to reach 20K stars. Star count depends on product quality, timing, distribution, and community adoption. The objective here is to remove objectively verifiable reasons a serious user, maintainer, or reviewer would reject the repository on first contact.

## 1. Evidence-based current state

### Verified strengths

- Clean environment checkpoint: 71 tests passing.
- Ruff lint, Ruff format, strict mypy, wheel/sdist build, and `twine check` pass in the declared validation environment.
- `/v1/models` normalizes rows, applies local allow/deny filters, and labels catalog-only rows as `unknown` rather than claiming they are live.
- `/v1/chat/completions` preserves arbitrary JSON fields, tools, response format, usage, errors, and arbitrary streaming byte boundaries in the current mock contract tests.
- Upstream authentication is process-owned rather than copied from an arbitrary client field.
- OmniRoute is the current upstream abstraction, routed through the loopback filtered boundary where configured.
- Public documentation explicitly discloses that managed intelligence, authentication, safe fallback, live availability, and end-to-end evidence remain open.

### Verified limitations

The following are facts from the current source, not inferred weaknesses:

- `llm_gate/api.py` constructs `Gate` directly and calls `gate_instance.route(...)` from the proxy path. There is no `IntelligenceService` boundary.
- `llm_gate/neural.py` still contains a historical `~/.9router/db/data.sqlite` path and direct SQLite access. It does not implement the documented Ruflo/RuVector protocol.
- `/ready` checks only process initialization and upstream `/models`. It does not check managed intelligence readiness, policy bundle state, or degraded-mode visibility.
- Chat requests are always classified with `criticality="medium"`; request policy, protected-task detection, capability requirements, and client override policy are not enforced on the proxy path.
- `UpstreamProxy` forwards to a configured URL but does not yet validate SSRF-sensitive URL configuration, authenticate the local caller, implement legal retry/fallback, or attach outcome records.
- Discovery swallows all failures and returns an empty list. The model data type defaults `is_available=True`, which is not a safe representation of an unverified catalog row.
- Explainability is currently a `RoutingDecision` with a reason string and limited fields. It does not expose policy version, candidate rejection reasons, score components, request fingerprint, safety flags, or outcome identity.
- The current test suite does not prove local authentication, SSRF protection, idempotency-aware fallback, protected-task rejection when intelligence is unavailable, redaction before adapters, Ruflo/RuVector command contracts, delayed quality outcomes, or clean install outside the source checkout.

## 2. What mature high-star repositories set as the bar

The comparison set used by the portfolio benchmark is: FastAPI, Pydantic, HTTPX, LiteLLM, Portkey Gateway, Express, Vercel AI SDK, OpenAI Node, Qlib, Backtesting.py, NautilusTrader, CCXT, Next.js, and Zustand. The exact star counts and snapshot date are recorded in the central portfolio benchmark, not repeated as permanent claims here.

A 20K-star attempt must meet the following practical bar, regardless of eventual star count:

| Dimension | Mature-repository expectation | llm-gate implication |
| --- | --- | --- |
| Product wedge | One sentence explains why the project exists and who uses it | Keep “best appropriate model through one stable gateway” as the wedge. Remove competing legacy language about only criticality tiers. |
| Installation | Fresh install works without source checkout assumptions | Add a release-candidate install harness, CLI smoke, mock upstream, and a documented OmniRoute path. |
| Protocol fidelity | Contract tests cover the real wire protocol, not only happy-path unit tests | Expand OpenAI-compatible models, chat, SSE, errors, tools, JSON mode, images, headers, cancellation, and retry boundaries. |
| Safety | Explicit auth, SSRF, secret handling, resource limits, and fail-closed behavior | Make local auth and upstream URL validation mandatory. Make protected requests reject when required intelligence is unavailable. |
| Correctness | Deterministic fixtures, property tests, and reproducible failure cases | Add policy and candidate-state fixtures, hard-gate property tests, and redacted evidence artifacts. |
| Operations | Health, readiness, metrics, logs, tracing, and clear degraded states | Readiness must include managed intelligence and policy state. Decision and outcome schemas must be stable. |
| Performance | Published benchmark methodology and regression thresholds | Benchmark classification, deterministic selection, proxy overhead, streaming throughput, and bounded adapter latency. |
| Extensibility | Stable interfaces and adapters instead of private coupling | Define versioned `IntelligenceService`, catalog, availability, outcome, and protocol adapter interfaces. |
| Documentation | README, examples, architecture, security, limits, and contributor path agree | Add a five-minute quickstart, production profile, degraded development profile, compatibility matrix, threat model, and demo transcript. |
| Community surface | Issues, labels, contribution rules, changelog, release cadence, and helpful triage | Keep Project 4 evidence linked to atomic issues. Publish only after every blocking claim has evidence. |

## 3. Prioritized gap register

| ID | Severity | Finding | Evidence | Required resolution | Ticket |
| --- | --- | --- | --- | --- | --- |
| G-01 | P0 | Mandatory intelligence is absent | `api.py` calls `Gate` directly; no `IntelligenceService` symbol; no Ruflo/RuVector calls | Implement typed service, deterministic safety floor, managed adapter, production readiness gate, explicit development degraded mode | #7 |
| G-02 | P0 | Adaptive layer violates the public design contract | `neural.py` reads historical 9router SQLite directly and has no real score source | Replace with documented subprocess/plugin adapter and local deterministic backend. Never read private databases | #7 |
| G-03 | P0 | Proxy path bypasses request policy | `chat_completions` always passes `criticality="medium"` and rewrites the client model without a policy decision envelope | Route every request through the service with task requirements, protected classification, capability gates, privacy policy, and explainability | #7, #8 |
| G-04 | P0 | Readiness is too weak | `/ready` checks only upstream `/models` | Add service, managed adapter, policy bundle, catalog, and upstream states. Return visible degraded status only for explicit development mode | #7, #9 |
| G-05 | P0 | Local caller authentication is missing | No auth dependency on HTTP routes; `start_server` defaults to `0.0.0.0` | Add loopback-safe anonymous development mode and bearer or Unix-socket production mode. Test unauthorized, authorized, and secret-redaction paths | #10 |
| G-06 | P0 | SSRF and unsafe upstream configuration are not proven | `UpstreamProxy` accepts a normalized string URL without scheme/host/private-network policy validation | Validate startup configuration, prohibit per-request destinations, document intentional loopback/private upstream exceptions, and test rejected schemes/hosts | #10 |
| G-07 | P0 | Retry and fallback contract is absent | `UpstreamProxy.chat` sends once and `api.py` returns 502 on exceptions | Add bounded retry policy with idempotency, `Retry-After`, pre-byte streaming boundary, same hard floor, and decision/event evidence | #8 |
| G-08 | P0 | Availability is not truthful in direct routing | Discovery swallows failures; `ModelInfo.is_available` defaults true; no health/headroom adapter on the proxy path | Use explicit `ready/degraded/unknown/denied` states and reject unknown rows by default for protected work | #9 |
| G-09 | P0 | Explainability and outcomes are insufficient | `RoutingDecision` has reason/tier but no policy version, candidate rejection set, score components, or delayed quality outcome | Add versioned redacted decision and outcome schemas with stable request IDs and no raw prompt by default | #7, #8 |
| G-10 | P0 | Compatibility coverage is too narrow | Current tests cover basic preservation and chunk boundaries only | Add contract matrix for malformed input, 429, timeout, tool deltas, JSON mode, images, cancellation, headers, usage, and upstream error bodies | #8 |
| G-11 | P0 | Clean install and live evidence are incomplete | Existing checks are source-tree quality checks; live OmniRoute quota truth is not established | Build a fresh venv/wheel smoke harness, mock upstream fixture, filtered OmniRoute smoke, and explicit limitation report for unavailable quota APIs | #10 |
| G-12 | P1 | Public product positioning is still split between alpha library and gateway | README and metadata correctly say alpha, but the core promise and supported matrix are not yet demonstrated | Replace broad marketing language with a sharp wedge, architecture diagram, reproducible demo, compatibility table, and limitations | #10 |
| G-13 | P1 | No performance or cost-quality evidence | No published benchmark for classifier, selection, adapter overhead, streaming, or fallback | Add reproducible benchmark fixtures and thresholds. Do not claim savings or quality without outcome data | #10 |
| G-14 | P1 | Operational UX is incomplete | No stable metrics surface, decision lookup, outcome submission, or structured redacted event review | Add minimal observability contract before adding a UI or autonomous suggestions | #7, #10 |
| G-15 | P2 | Suggestion capability is not yet a release feature | Issue #11 is correctly deferred | Implement only after validated outcome data exists. Suggestions remain advisory and approval-gated | #11 |

## 4. Target architecture

The implementation target is a two-layer router with a strict boundary:

1. **Protocol boundary** parses OpenAI-compatible requests, authenticates local callers, enforces size/time limits, and never trusts a client-selected upstream URL.
2. **IntelligenceService** is the only route-selection entrypoint. It returns a typed decision envelope and runs deterministic checks on every request.
3. **Deterministic safety floor** classifies task requirements, applies hard policy, validates capabilities, evaluates candidate availability, and builds redacted explanations.
4. **Managed adaptive adapter** invokes only documented Ruflo/RuVector surfaces with bounded timeouts and redacted versioned JSON. It can rank eligible candidates or record outcomes, but cannot bypass hard gates.
5. **Catalog and availability service** normalizes OmniRoute or another compatible upstream into explicit candidate states. A catalog listing is not proof of readiness or quota.
6. **Dispatch policy** rewrites only the model selection, preserves forward-compatible request fields, and enforces retry/fallback legality before any response bytes are emitted.
7. **Outcome service** records transport outcome immediately and accepts delayed validated quality outcome separately. HTTP 200 must never be treated as quality success automatically.
8. **Readiness and observability** expose policy version, backend state, degraded mode, counters, and safe request IDs without raw prompts, completions, credentials, or private database reads.
9. **SuggestionService** remains an asynchronous P2 consumer of validated aggregates. It cannot mutate policy, invoke tools, or change readiness without an approved tracked work item.

A request must fail closed for protected work when required managed intelligence is unavailable. Non-protected work may use the deterministic floor only when the operator explicitly enables development degraded mode, and every response/event must say so.

## 5. Staged lift and routing plan

### Wave A: architecture and safety boundary, P0

- **#7, assigned design/review:** `gpt-5.6-luna`.
- **#7, implementation:** `gpt-5.4-mini` or equivalent capable lower-cost coding route after the protocol is frozen.
- Deliver the typed interfaces, adapter protocol, deterministic floor, readiness matrix, redaction, protected rejection, and property tests.
- Review gate: Luna or another frontier reviewer checks policy authority, privacy, subprocess safety, and failure semantics.

### Wave B: protocol and dispatch correctness, P0

- **#8 implementation:** lower-cost capable coding route such as `gpt-5.4-mini` or `agy/gemini-3.5-flash-medium`.
- Deliver auth-aware proxy, model rewrite contract, SSE/error/header preservation, bounded retry/fallback, request cancellation, and mock-upstream compatibility matrix.
- Review gate: frontier review only for protocol ambiguity, auth, SSRF, and hard-gate changes.

### Wave C: catalog and availability truth, P0

- **#9 implementation:** `gpt-5.4-mini` or `agy/gemini-3.5-flash-medium`.
- Deliver explicit candidate state, capability metadata, bounded health/headroom adapters, shared filtered upstream configuration, and stale 9router cleanup.
- Review gate: verify that no catalog row or stale quota signal is represented as ready.

### Wave D: release, packaging, and public proof, P0/P1

- **#10 implementation:** `agy/gemini-3.5-flash-low`, OpenRouter coding route, or another lower-cost model with a deterministic checklist.
- Deliver fresh-install harness, package manifest inspection, CLI smoke, security scans, benchmark fixtures, compatibility matrix, README/demo/claims reconciliation, and release notes.
- Review gate: frontier release adjudication. No public promotion if any blocking evidence is missing.

### Wave E: evidence-backed suggestions, P2

- **#11 implementation:** lower-cost model only after Wave A-D are verified.
- Require sufficient validated outcomes, privacy-safe aggregation, expiry, deduplication, and approval workflow before exposing suggestions publicly.

## 6. Objective release gate

The flagship cannot be called production-ready, 20K-star quality, or publicly promoted until all of these are evidenced:

- Fresh wheel/sdist install outside the source checkout works.
- All declared tests, Ruff, strict mypy, build, package, and dependency/security checks pass.
- Auth and SSRF tests pass, with no credentials or raw prompts in logs/events/artifacts.
- Every routed request passes through `IntelligenceService`.
- Production readiness fails closed when managed Ruflo/RuVector intelligence is unavailable.
- Explicit development degraded mode is visible and cannot be confused with production readiness.
- Protected requests reject or select an allowlisted model when hard requirements cannot be satisfied.
- Catalog rows are not called ready without bounded health/headroom evidence.
- Retry/fallback tests prove no unsafe downgrade, duplicate non-idempotent request, or post-byte switch.
- OpenAI client, raw HTTP client, CLI-agent configuration, mock upstream, and filtered OmniRoute smoke are reproducible.
- Decision explanations and outcomes are versioned, redacted, deterministic, and inspectable.
- Benchmarks publish methodology, environment, thresholds, and limitations.
- README, metadata, examples, changelog, and issue state match the evidence exactly.
- A frontier reviewer has inspected the final diff and release evidence.

## 7. Non-goals and anti-patterns

- Do not optimize for cheapest model when a higher-quality eligible model is required.
- Do not treat SONA, Ruflo, or any learned signal as policy authority.
- Do not read OmniRoute, Ruflo, or RuVector private databases from the public package.
- Do not add provider-specific protocol adapters before the OpenAI-compatible contract is solid.
- Do not claim universal framework interception. A protocol proxy can serve many tools, but cannot transparently intercept clients that do not point at it.
- Do not use star count, benchmark numbers, or HTTP success as a substitute for product evidence.
- Do not promote to Hacker News or other communities until the blocking gate is Verified.

## 8. Review questions for every future change

1. What user-visible behavior changes?
2. Which hard policy or capability gate does it touch?
3. Can a lower-cost model implement it without making a release/security decision?
4. What deterministic fixture or property test proves it?
5. What fresh-install or live smoke proves it outside the source checkout?
6. What exact GitHub issue, Project 4 state, commit, and evidence field record it?
7. Could the change leak credentials, raw prompts, provider internals, or unsupported claims?

**Current conclusion:** llm-gate is a promising flagship foundation, not a launch candidate. The next correct move is Wave A design freeze and bounded implementation, followed by adversarial review and reproducible release evidence. Promotion remains blocked.
