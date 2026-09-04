# Feature Specification: Routing Demo Cost vs Quality

**Feature Branch**: `feat/278-routing-demo`

**Created**: 2026-08-30

**Status**: Draft

**Input**: GitHub issue #278 — runnable demo that routes exactly 100 heterogeneous requests and displays cost/quality tradeoffs and savings so a potential user understands Verdict’s value without reading code. Confirmed constraints: savings numbers must come from live or recorded real catalog-and-execute evidence (not invented fixtures as the demo); if the live routing surface is unavailable, report blocked rather than faking a 100-request savings story; reuse the existing cheaper-first live-routing spend preference (do not invent a competing policy); do not implement Spec 272 Phase 3 paired eval or ADK work.


## Clarifications

### Session 2026-08-30

- Q: Where should the runnable demo and docs live given issue text (`examples/routing-demo/`) versus independent ownership (`tests/`, `docs/benchmarks/`, or `verdict/routing_demo.py`)? → A: Own `verdict/routing_demo.py` as the library/entrypoint core, put reviewer docs under `docs/benchmarks/routing-demo.md`, and put focused tests under `tests/test_routing_demo*.py`. A thin `examples/routing-demo/` wrapper MAY be added only if needed for issue-path discoverability; do not touch Spec 272 Phase 3 files.
- Q: How can ~100 requests finish in under 60 seconds while still basing savings on real catalog+execute rather than invented fixtures? → A: Fetch a live catalog once (or load a labeled recorded capture). Apply cheaper-first routing to all 100 requests using that catalog’s published prices for routed vs named expensive baseline costs. Perform real bounded executes for success/latency evidence (short max-token checks on chosen routes). Do not invent model rows or prices. If the live surface is down and no recorded capture is opted into, report blocked.
- Q: What is the named expensive baseline for savings? → A: For each request, the costliest still-qualified identity that could serve that request; aggregate baseline is the sum of those per-request baseline costs on the same request set.
- Q: Exact request count? → A: Exactly 100 requests per demo run (fixed mix of simple and complex classes).
- Q: How is recorded replay requested so it cannot be mistaken for live? → A: Only via an explicit recorded-capture option/path; output must label mode as `recorded` with capture identity/time. Default mode is live; live failure without that option is blocked.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run a portfolio-readable routing demo (Priority: P1)

A potential user or reviewer runs a single documented demo command. The demo processes exactly 100 heterogeneous requests (mix of simple and complex intents), shows each request’s routing decision with a short rationale, and finishes with aggregate cost comparison versus a clear expensive baseline plus quality signals (latency and success rate). The reviewer can grasp the savings story without opening source files.

**Why this priority**: This is the portfolio showcase acceptance for issue #278. Without a runnable, explainable demo, the cost/quality value proposition stays theoretical.

**Independent Test**: From a clean checkout with the documented prerequisites, run the demo once. Confirm exactly 100 requests are processed, per-request decisions with rationales are visible, aggregate cost vs baseline and quality metrics are visible, and total wall time is under 60 seconds when the live surface is healthy.

**Acceptance Scenarios**:

1. **Given** documented prerequisites are met and the live routing surface is healthy, **When** the reviewer runs the documented demo command, **Then** exactly 100 heterogeneous requests are processed and a readable summary is produced without reading source.
2. **Given** a completed demo run, **When** the reviewer inspects per-request output, **Then** each request names the chosen route and a short rationale for that choice.
3. **Given** a completed demo run, **When** the reviewer inspects the aggregate section, **Then** total routed cost, baseline cost, and savings (absolute and relative) are shown using numbers derived from real catalog-and-execute evidence for that run or a recorded replay of such a run.
4. **Given** a completed demo run, **When** the reviewer inspects quality metrics, **Then** latency and success rate for the routed path are shown alongside the cost comparison.
5. **Given** a healthy live surface, **When** the demo completes, **Then** wall-clock duration is under 60 seconds.

---

### User Story 2 - Prefer cheaper qualified spend in the demo path (Priority: P1)

When cheaper qualified routes can serve a request, the demo’s routed path must not spend a more expensive route solely for convenience. The spend preference must match the existing live-routing cheaper-first behavior already proven for the golden path—not a parallel invented policy.

**Why this priority**: Savings claims are only credible if selection obeys the same cheaper-first rule users are asked to trust.

**Independent Test**: On a catalog that includes both cheaper and paid qualified options for a simple request class, confirm the demo’s routed choice prefers the cheaper qualified option and the rationale states that preference; paid is used only when no cheaper qualified option remains.

**Acceptance Scenarios**:

1. **Given** a request that at least one cheaper qualified route can serve, **When** the demo routes that request, **Then** a paid-only choice is not used while that cheaper option remains.
2. **Given** only paid qualified routes remain for a request, **When** the demo routes that request, **Then** a paid route may be used and the rationale states that no cheaper qualified option existed.
3. **Given** the same catalog capture, request mix, and policy, **When** the demo is run twice, **Then** keep/drop/spend conclusions for comparable requests match.

---

### User Story 3 - Honest evidence when the live surface is down (Priority: P1)

If the live gateway/provider surface cannot be reached, the demo must fail closed as **blocked** for the live savings story. It must not invent fixture prices or synthetic 100-request savings and present them as the demo proof. A separately recorded replay of a prior real catalog-and-execute capture may be shown only when clearly labeled as recorded evidence, never as a live pass.

**Why this priority**: A fake savings chart destroys portfolio trust. Issue constraints forbid vaporware fixtures as the demo.

**Independent Test**: With the live surface unreachable and no labeled recorded capture provided, run the demo and confirm the outcome is blocked (non-zero / explicit blocked status) with no aggregate savings chart claimed as live proof.

**Acceptance Scenarios**:

1. **Given** the live routing surface is unreachable and no recorded real capture is supplied, **When** the demo runs, **Then** it reports blocked and does not claim a completed 100-request live savings comparison.
2. **Given** a labeled recorded capture produced earlier from real catalog-and-execute, **When** the operator opts into replay, **Then** savings numbers are shown as recorded evidence with capture identity/time, not as a live run.
3. **Given** either live or recorded mode, **When** outputs are inspected, **Then** they contain no secrets, credentials, raw prompts, or completions.

---

### Edge Cases

- Mixed request difficulty: simple requests must not all escalate to the expensive baseline; complex requests may legitimately need costlier routes when cheaper ones are unqualified.
- Empty or unhealthy catalog: treat as blocked for the live savings story; do not backfill with invented models.
- Partial execute failures: success rate must reflect real failures; do not count failed executes as successful savings.
- Baseline definition missing: the demo must name the baseline (for example “always choose the most expensive qualified route that could serve each request”) so savings are interpretable.
- Duration pressure vs honesty: missing the 60-second target is a performance defect; inventing faster fixture math is not an allowed fix.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The product MUST provide a documented, runnable routing demo that processes exactly 100 heterogeneous requests (simple and complex) in one invocation.
- **FR-002**: The demo MUST display a per-request routing decision that names the chosen route and a short human-readable rationale.
- **FR-003**: The demo MUST display aggregate cost for the routed path, aggregate cost for the named expensive baseline (per request: costliest still-qualified identity that could serve it; aggregate: sum over the same 100 requests), and the resulting savings (absolute and percentage).
- **FR-004**: Savings and cost figures MUST be derived from live catalog-and-execute evidence or from a clearly labeled recorded replay of such evidence; invented fixture catalogs MUST NOT be presented as the demo’s savings proof.
- **FR-005**: The demo MUST display quality metrics covering at least latency and success rate for the routed path.
- **FR-006**: When the live routing surface is healthy, a full demo run MUST complete in under 60 seconds wall clock.
- **FR-007**: When the live routing surface is unreachable and no recorded real capture is opted into, the demo MUST report blocked and MUST NOT emit a fake live 100-request savings comparison.
- **FR-008**: Routed spend preference MUST reuse the existing live-routing cheaper-first behavior; the demo MUST NOT introduce a competing selection policy.
- **FR-009**: Documentation MUST explain prerequisites, how to run the demo, how to interpret baseline vs routed savings, and what “blocked” means.
- **FR-010**: Demo outputs MUST omit secrets, credentials, raw prompts, and completions.
- **FR-011**: This feature MUST NOT implement Spec 272 Phase 3 paired context ablation or ADK integration; those remain other stories.

### Key Entities *(include if feature involves data)*

- **Demo Request**: One heterogeneous unit of work in the ~100-request mix; has difficulty/class, and later a chosen route and rationale.
- **Routing Decision Record**: Per-request chosen route, rationale, cost contribution, latency, and success/failure.
- **Baseline Comparison**: Named expensive alternative cost for the same request set used to compute savings.
- **Demo Summary**: Aggregate routed cost, baseline cost, savings, latency, success rate, mode (live vs recorded), and blocked/completed status.
- **Evidence Capture**: Live run receipt or labeled recorded replay identity that justifies the numeric claims.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A reviewer unfamiliar with the codebase can run the documented demo and, within one sitting, correctly state whether routed spend beat the named baseline and by roughly how much.
- **SC-002**: A healthy-surface demo processes exactly 100 requests and finishes in under 60 seconds.
- **SC-003**: In a side-by-side check, demo savings numbers match the underlying live or recorded catalog-and-execute evidence for that run (no unexplained invention).
- **SC-004**: With the live surface down and no recorded capture, 100% of demo attempts report blocked rather than a fabricated savings chart.
- **SC-005**: For request classes where a cheaper qualified route exists, the demo’s routed path does not select a costlier route while that cheaper option remains available.
- **SC-006**: Per-request rationales are understandable to a non-developer reviewer for at least a sample of 10 inspected decisions.

## Assumptions

- The primary actor is a potential user or portfolio reviewer, not only a Verdict maintainer.
- Each demo run processes exactly 100 requests (fixed mix of simple and complex classes), not a free-form load test.
- Heterogeneous means the mix includes both simple and complex request classes so cheaper-first has something to optimize.
- The expensive baseline is the costliest still-qualified identity that could serve each request; aggregate baseline sums those per-request costs.
- Existing live-routing golden-path cheaper-first and catalog fetch behavior are reused as the spend authority; this feature adds the multi-request demo and comparison narrative.
- OmniRoute-compatible local gateway is the default live surface when present; absence yields blocked for live mode.
- Recorded replay is optional supporting evidence, never a silent substitute claiming to be live.
- Spec 272 Phase 3 (paired eval / context ablation) and ADK are out of scope.
- Demo ownership: `verdict/routing_demo.py`, `docs/benchmarks/routing-demo.md`, and `tests/test_routing_demo*.py`, without conflicting with Spec 272 Phase 3 files. A thin `examples/routing-demo/` wrapper is optional for path discoverability only.
