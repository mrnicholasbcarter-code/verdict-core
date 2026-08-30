# Feature Specification: Live Routing Golden Path

**Feature Branch**: `276-live-routing-golden-path`

**Created**: 2026-08-30

**Status**: Draft

**Input**: User-confirmed remaining product: prove live model discovery, probing, qualification, selection, explanation, and one real bounded execution against a live gateway or provider; preserve paid usage; fixture catalogs are rule tests only; portfolio career observation does not block.

## Provenance

This specification is the durable remainder of the original product intent, confirmed 2026-08-30:

- ChatGPT export `~/OpenAI-export.zip` conversation titled **Verdict** (2026-08-01): employment-proof product plus “generate stories with acceptance criteria”; later milestone “prove live model discovery, probing, qualification, selection, explanation, and one real bounded execution.”
- ChatGPT conversation **Unified Context Router**: evaluate available models and use an appropriate model so paid usage is preserved or reduced.
- Spec Kit features `001`–`012` and `272`–`275` already exist; this feature does not replace them. It names the missing heart that later remainder lists kept losing.
- Confirmed constraints: paid-usage preservation is hard; the demo must use a live gateway or provider (fixture pass is vaporware); LinkedIn/career observation from `specs/012-portfolio-mvp-launch` stays user-gated and does not block this feature. Core remains policy authority; the gateway only executes chosen identities.

## Clarifications

### Session 2026-08-30

- Q: If a model’s cost class is unknown, may it be selected, and can it count as a cheaper alternative that blocks paid spend? → A: Do not guess. Fetch published model specifications from the gateway or provider catalog (the same class of facts a routing gateway already syncs: identity, pricing, context, tools, modalities). Apply that fetch to every pending attribute, not only cost. Any required fact that cannot be fetched remains unclassified and is excluded until classified. Catalog membership alone is not qualification.
- Q: May a mix (combo, fallback chain, or similar) be selected as a route, and under what visibility rule? → A: Allow inspectable mixes of named qualified steps; deny opaque automatic pickers. Cost class of the mix is the cost class of the first remaining qualified step that would actually run.
- Q: What counts as the one bounded, useful unit of work that must be executed and independently verified? → A: One named pre-stated check with a pass/fail the operator can understand without source (produce a required artifact that a known checker accepts or rejects). Small enough to rerun. Not an open-ended session. Not “any model reply.”
- Q: How many times may execution fail over to another qualified route before the bounded unit is reported failed? → A: Keep failing over, including paid routes, until something succeeds. Order remains cheaper-first: do not use a paid route while a cheaper qualified unused route remains. Do not retry the same identity. If every remaining unique qualified identity fails, fail closed. The receipt records every attempt.
- Q: How fresh must fetched model specifications be before a candidate may be selected? → A: Use a catalog captured for this run, or one still inside an operator-declared freshness window. Older facts are unclassified and excluded.
- Q: Does a provided/fixture catalog count as proving the golden path? → A: No. The golden path is not done unless a live gateway or provider catalog is fetched and the named check is executed on a real selected identity. Fixture catalogs are only for rule tests. Adapter absence is blocked, not a green demo.
- Default (analysis remediation, 2026-08-30): Named check is exactly JSON `{"golden_path":"ok"}`. Cost rank is local, then free, then cheaper, then paid; ties break on identity_id. Live “denied” is a policy denylist on the live listing. Cost class comes from the catalog row, else a same-capture pricing listing, else unclassified.
- Default (usage surface, 2026-08-30): Do not depend on CodexBar or Codex Toolbar. Detect each provider the same way those apps do: find well-known local credential JSON/env for that tool, then fetch that provider’s documented usage endpoint. Allowlisted remaining-quota facts only. Do not persist or receipt secrets. Do not mutate credential files. Cookie/browser scraping is out of scope unless later opted in. Missing credentials for a provider skip that provider’s quota signal; they do not pass the golden path by themselves. Exhausted remaining quota MUST NOT be treated as cheaper/free.

## Discovery model *(mandatory)*

The product must detect four distinct kinds of thing. Mixing them is how routing becomes inelegant.

| Kind | What it is | What it is not |
|------|------------|----------------|
| **Gateway** | A live transport that can list identities and execute a chosen identity (OpenAI-compatible family such as a local OmniRoute endpoint). | The policy authority. A fixture file. |
| **Provider** | A connected account or backend behind a gateway (OAuth provider, API-key provider, local runtime). | A model. |
| **Concrete identity** | A resolved model whose specifications have been fetched: identity, cost class, context/output limits, tools, modalities. | An alias, pool, or automatic picker. |
| **Mix** | An optional inspectable sequence of concrete identities (gateway “combo”, fallback chain, or similar). Each step is still a concrete identity. | A neural mixture-of-experts, an opaque `auto/*` picker, or a virtual factory whose chosen step cannot be named. |

Rules that keep this elegant:

1. Discover gateways, then providers, then identities, then (optionally) mixes — never the reverse.
2. Fetch specifications; do not infer cost, capability, or context from names, tiers, or task-keyword heuristics.
3. Qualify each concrete identity independently. A mix is qualified only if every named step is qualified.
4. Opaque automatic routing is not a candidate. If the resolved step cannot be named before execution, drop it.
5. Cost class comes from fetched pricing facts, not from alias branding.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Discover and filter available models (Priority: P1)

An operator asks the system which models may be used for a bounded piece of work. The system fetches the live gateway or provider catalog, probes health and capability, and returns only candidates that pass policy and qualification. Denied, unhealthy, and unqualified models never appear as selected. Fixture catalogs may be used only in classification-rule tests, not as the demonstration.

**Why this priority**: Without a truthful candidate set, later selection is theater. This is independently valuable even if no execution happens.

**Independent Test**: On a live listing, apply a policy denylist for “denied,” use probe failure for “unhealthy,” missing specs for “unqualified,” and at least one fully classified healthy identity. Confirm only qualified healthy models remain and each drop has a reason. Classification-rule tests may use fixtures for the same four classes.

**Acceptance Scenarios**:

1. **Given** a catalog with mixed healthy, unhealthy, denied, and unqualified models, **When** discovery runs for a bounded task, **Then** only qualified healthy models remain as candidates.
2. **Given** a denied or policy-blocked model, **When** discovery completes, **Then** that model is not selected and the exclusion names the policy reason.
3. **Given** an unhealthy or unresponsive model, **When** discovery completes, **Then** that model is not selected and the exclusion names health or probe failure.
4. **Given** a model that cannot satisfy the task’s required capabilities, **When** discovery completes, **Then** that model is not selected and the exclusion names the missing capability.
5. **Given** a successful discovery, **When** the operator inspects the result, **Then** every kept and dropped model is identifiable without reading internal implementation.

---

### User Story 2 - Prefer cheaper qualified routes over paid (Priority: P1)

When at least one qualified cheaper, free, or local candidate can do the work, the system must not spend a paid model. Paid models are used only when no cheaper qualified candidate exists.

**Why this priority**: This is the original product reason for routing. Selection that ignores cost is a failed feature even if it “picks something.”

**Independent Test**: Present a catalog with one qualified free/local/cheap candidate and one paid candidate that would also pass. Run selection. Confirm the paid candidate is not used and the explanation states that a cheaper qualified candidate existed.

**Acceptance Scenarios**:

1. **Given** at least one qualified cheaper/free/local candidate and one paid candidate, **When** a route is selected, **Then** the paid candidate is not used.
2. **Given** no cheaper qualified candidate and a qualified paid candidate, **When** a route is selected, **Then** the paid candidate may be used and the explanation states that no cheaper qualified option existed.
3. **Given** only paid candidates that fail qualification, **When** selection runs, **Then** no route is selected and the system reports that no qualified candidate exists rather than weakening policy to spend.
4. **Given** two cheaper qualified candidates, **When** selection runs, **Then** the chosen cheaper candidate is deterministic for the same catalog, task, and policy.

---

### User Story 3 - Explain keep, drop, and spend decisions (Priority: P1)

A reviewer who is not a developer can tell why a model was kept, dropped, or skipped for cost, using only the explanation the system produces.

**Why this priority**: Employment-proof and trust both require explainability. An unexplained pick cannot be demonstrated.

**Independent Test**: Run discovery and selection on a mixed catalog. Give a reviewer only the explanation artifact. The reviewer can correctly identify at least one kept model, one dropped model, and whether a paid model was used, without source code.

**Acceptance Scenarios**:

1. **Given** a completed selection, **When** a reviewer reads the explanation, **Then** each dropped candidate has a human-readable reason (policy, health, capability, or cost).
2. **Given** a completed selection, **When** a reviewer reads the explanation, **Then** the chosen candidate is named and the reason it beat remaining candidates is stated.
3. **Given** an explanation, **When** it is inspected, **Then** it contains no secrets, credentials, raw prompts, or completions.
4. **Given** the same catalog, task, and policy twice, **When** explanations are compared, **Then** keep/drop/spend conclusions match.

---

### User Story 4 - One bounded execution with evidence (Priority: P1)

The operator runs one named, pre-stated check through the selected route. A known checker accepts or rejects the required artifact. The system records the outcome and emits an evidence receipt that binds the selection, the work, and the result. This is the golden-path demonstration. An open-ended session or an unchecked model reply does not count.

**Why this priority**: Discovery without doing work does not prove the product. One verified execution is the employment-proof demo.

**Independent Test**: After a successful selection, execute one bounded task with a known expected result. Confirm the work ran on the selected route (or an explicitly recorded failover that still obeys Stories 1–2), the result is independently checked, and a receipt exists that a reviewer can inspect without source code.

**Acceptance Scenarios**:

1. **Given** a selected qualified route, **When** the operator runs the bounded unit, **Then** the work executes on that route unless a recorded failover occurs.
2. **Given** a failover, **When** the replacement route is chosen, **Then** cheaper unused qualified identities are tried before paid ones, the same identity is not retried, and the receipt records every attempt until success or exhaustion.
3. **Given** a completed run, **When** verification runs, **Then** pass or fail is explicit; a failed verification is not reported as success.
4. **Given** a completed run, **When** a reviewer inspects the receipt, **Then** it names the task, selected route, cheaper-vs-paid decision, verification outcome, and source identity of the work.
5. **Given** a receipt, **When** it is inspected, **Then** it contains no secrets, credentials, raw prompts, or completions.

---

### User Story 5 - Live gateway or provider is required for the demo to pass (Priority: P1)

The golden path is proven only against a real gateway, provider, or equivalent live catalog+execute surface. A hand-built fixture catalog is not the demo. If the live surface is unreachable, the run is blocked and reported as blocked — not passed.

**Why this priority**: A fixture-only pass is vaporware. Employment proof needs a real fetch and a real selected identity doing the named check.

**Independent Test**: Point at a live gateway or provider. Fetch its catalog. Select cheaper-first from fetched specs. Execute the named check on the selected identity. The receipt names the live endpoint, the resolved identity, and the checker result. Repeating the same steps with the live surface down yields blocked, not success.

**Acceptance Scenarios**:

1. **Given** a reachable live gateway or provider, **When** the golden path runs, **Then** catalog rows come from that live listing, not from a committed fixture used as the source of truth.
2. **Given** that live run, **When** the named check executes, **Then** it is sent to the selected concrete identity through that gateway or provider, and the checker pass/fail is recorded.
3. **Given** the live surface is down, **When** the golden path runs, **Then** the result is blocked (not passed), with a reason that live discovery/execution was unavailable.
4. **Given** a fixture catalog, **When** it is used, **Then** it may exercise classification rules in tests, but it MUST NOT be reported as the golden-path demonstration.

---

### User Story 6 - Cookie/browser usage probes (Priority: P3, later)

After the live golden path works from credential files and usage APIs, a later phase MAY add CodexBar-style browser-cookie strategies (Cursor, Claude web extras, Copilot budget extras). This phase is not required for US1–US5.

**Why this priority**: File+OAuth usage already improves cheaper-vs-paid. Cookie import needs Full Disk Access, is Mac-browser specific, and must stay opt-in.

**Independent Test**: With cookies opted in, a provider that has no CLI credential file still yields an allowlisted remaining-quota snapshot. With cookies off, that provider is skipped. Secrets still never appear on the receipt. US5 still requires live gateway execute.

**Acceptance Scenarios**:

1. **Given** cookie probing is disabled (default), **When** golden path runs, **Then** no browser cookie stores are read.
2. **Given** cookie probing is opted in and a supported cookie exists, **When** usage is fetched, **Then** remaining quota is allowlisted and exhausted quota still cannot stay cheaper.
3. **Given** cookie probing fails, **When** the run continues, **Then** that provider’s quota signal is skipped; the live catalog+execute path is unchanged.

---

### Edge Cases

- Opaque automatic picker or unnameable mix step: drop; do not wait for post-run identity.
- Mix whose first remaining qualified step is paid, even if a later step is cheaper: treat as paid; do not select it while a cheaper qualified concrete identity or cheaper-first mix exists.
- Failover: cheaper unused qualified identities first; paid only after that set is empty; no retry of the same identity; stop on first success or when the unique qualified set is exhausted.
- Specification fetch fails or is outside the declared freshness window: treat required fields as unclassified; exclude the candidate; do not guess. Identity-name match MUST NOT override staleness.
- Empty catalog: no selection; report that no candidates were available.
- All candidates fail policy, health, or capability: no selection; do not weaken requirements to escape the block.
- Catalog identity conflict (same name, different live identity): fail closed; do not silently pick.
- Stale catalog or expired probe: do not treat as currently qualified.
- Cheaper candidate that is qualified but slower: still preferred over paid unless the task’s stated time bound makes the cheaper candidate unqualified.
- Bounded work that cannot be verified: do not emit a successful receipt.
- Partial adapter failure (catalog works, execution transport fails): do not report golden-path pass; receipt names `live_surface_blocked` or `checker_failed` as applicable.
- Live “denied” class: operator policy denylist (and gateway-disabled ids) applied to the live listing, not a fake catalog of fake models.
- Usage snapshot says remaining quota is exhausted: drop for health/quota; do not keep as cheaper.
- Usage surface missing: ignore for selection; still require live catalog fetch.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST discover candidate models by fetching a live gateway or provider catalog. Fixture catalogs MUST NOT be the source of a demonstration receipt.
- **FR-001a**: The system MUST distinguish gateway, provider, concrete identity, and mix, and MUST NOT treat an alias, pool, or automatic picker as a concrete identity.
- **FR-001b**: The system MUST fetch published specifications for each candidate (identity, cost class, context/output limits, tools, modalities) from the live catalog row. If cost class is absent on the row, the system MAY fetch a gateway pricing listing for the same capture; if still absent, the field is unclassified. Guessing from names or keyword task detection is forbidden.
- **FR-001c**: Any required specification field that cannot be fetched MUST remain unclassified. Unclassified candidates MUST be excluded from selection and MUST NOT count as cheaper alternatives or as paid justifications.
- **FR-001d**: A mix MAY be selected only when every step is a named concrete identity whose specifications were fetched and independently qualified. Opaque automatic pickers MUST be dropped.
- **FR-001e**: The cost class of a mix is the cost class of the first remaining qualified step that would actually run. A cheaper unused later step MUST NOT make a paid first step count as cheaper.
- **FR-001f**: Fetched specifications MUST be from this run’s live catalog capture or still inside an operator-declared freshness window. Facts outside that window MUST be treated as unclassified and excluded. Fixture catalogs used in rule tests MUST carry a capture time but MUST NOT satisfy FR-014.
- **FR-001g**: Among kept identities, selection order MUST be `local`, then `free`, then `cheaper`, then `paid`. Ties MUST break on `identity_id` lexical order.
- **FR-001h**: The system MUST discover per-provider usage the same way a menu-bar usage app does, without depending on that app: locate well-known local credential files or environment tokens for each known provider, then fetch that provider’s documented usage endpoint. Receipts MUST contain only allowlisted remaining-quota facts (percent used, remaining, reset time, provider id). Secrets, cookies, and access tokens MUST NOT be persisted or receipted. The system MUST NOT write or refresh tokens into those credential files. Cookie/browser-session scraping is a later phase (US6), not required for the golden-path demonstration. If a provider has no local credentials, skip that provider’s quota signal in this phase. Exhausted remaining quota MUST NOT keep a candidate as `free`/`local`/`cheaper`. Usage discovery MUST NOT by itself satisfy FR-014.
- **FR-002**: The system MUST probe candidate health and required capability on a bounded live sample before a candidate may be selected. Unknown or failed probe status MUST NOT be treated as healthy.
- **FR-003**: The system MUST exclude denied, policy-blocked, unhealthy, unresponsive, stale, and unqualified candidates from selection.
- **FR-004**: The system MUST record a specific exclusion reason for every dropped candidate.
- **FR-005**: The system MUST NOT select a paid model when at least one cheaper, free, or local candidate is qualified for the same task.
- **FR-006**: The system MUST select a paid model only when no cheaper qualified candidate exists, and MUST state that fact.
- **FR-007**: The system MUST refuse to select a route rather than weaken policy, capability, or verification to spend or to finish.
- **FR-008**: Selection for the same catalog, task, and policy MUST be deterministic.
- **FR-009**: The system MUST produce an explanation a non-developer can use to see who was kept, who was dropped, why, and whether paid spend occurred.
- **FR-010**: Explanations and receipts MUST NOT contain secrets, credentials, raw prompts, or completions.
- **FR-011**: The system MUST execute one named, pre-stated bounded unit through the selected live identity. The unit is: ask the identity to reply with only the JSON object `{"golden_path":"ok"}`. The independent checker parses that object and passes only if `golden_path` equals `ok`. Any other reply, including extra keys, prose, or unparseable text, MUST fail. An unchecked model reply MUST NOT count as the unit.
- **FR-011a**: On execution failure the system MUST fail over through remaining unique qualified identities, cheaper-first, including paid identities only after no cheaper qualified unused identity remains, until one attempt succeeds or the set is exhausted. The same identity MUST NOT be retried. Exhaustion MUST fail closed. Every attempt MUST be on the receipt.
- **FR-012**: The system MUST independently verify the bounded unit’s outcome and MUST NOT report a failed or unverifiable outcome as success.
- **FR-013**: The system MUST emit an evidence receipt binding task identity, selected route, cheaper-vs-paid decision, verification outcome, and source identity of the work.
- **FR-014**: The golden-path demonstration MUST fetch the catalog from a live gateway or provider and MUST execute the named check on a real selected identity through that surface.
- **FR-015**: If the live surface is unreachable, the demonstration MUST be blocked and MUST NOT be reported as passed. Fixture catalogs MAY be used only to test classification rules and MUST NOT satisfy SC-004.
- **FR-016**: Career, LinkedIn, and other portfolio observation tasks from the existing portfolio closeout MUST NOT block this feature.
- **FR-017**: Cookie/browser-session usage probes are a later phase. They MUST be opt-in, MUST NOT be required for FR-014, MUST NOT persist cookies, and MUST NOT put secrets on receipts. Default is off.

### Key Entities

- **Catalog**: Fetched gateway/provider listing of identities plus their published specifications (not a hand-labeled nickname list).
- **Gateway / Provider / Concrete identity / Mix**: As defined in Discovery model.
- **Candidate**: One catalog entry after probe and qualification, marked kept or dropped with a reason.
- **Route selection**: The chosen candidate plus the cheaper-vs-paid decision and the explanation of remaining alternatives.
- **Bounded work unit**: Ask the selected live identity to reply with only `{"golden_path":"ok"}`. The checker passes iff that JSON parses and `golden_path` is `ok`.
- **Evidence receipt**: The inspectable record of selection, execution, verification, live endpoint, and source identity, with no secrets.
- **Live surface blocked**: Result when the gateway or provider cannot list or execute. This is not a pass.
- **Usage snapshot**: Allowlisted remaining-quota facts for a provider (percent used, reset time, credits remaining). Not credentials. Not a catalog.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a mixed catalog of at least four models (denied, unhealthy, unqualified, qualified), 100% of denied, unhealthy, and unqualified models are excluded from selection on every repeated run of the same inputs.
- **SC-002**: When a qualified cheaper/free/local candidate exists, paid-model use is 0% for that run.
- **SC-003**: A reviewer who is not a developer, given only the explanation, correctly identifies the chosen model, at least one exclusion reason, and whether paid spend occurred, in under 5 minutes.
- **SC-004**: One bounded work unit completes with an independent pass/fail check and a receipt that names task, route, cheaper-vs-paid decision, and verification outcome.
- **SC-005**: A reviewer can name the live gateway or provider endpoint, the resolved identity, and the checker outcome from the receipt. A run with that surface down is blocked, not passed.
- **SC-006**: Repeating the same catalog, task, and policy twice produces the same keep/drop set and the same cheaper-vs-paid decision.

## Assumptions

- Existing eligibility, envelope, and evidence work remains in force; this feature does not weaken those rules.
- “local” / “free” / “cheaper” / “paid” are fetched cost classes. Selection rank is local, then free, then cheaper, then paid; ties break on identity_id.
- The bounded work unit is small enough for a reviewer to understand (one named check, not an open-ended agent session).
- Live catalog access and live execution are required to prove the product. A provided catalog is only a rule-test aid.
- Feature `012` portfolio closeout, including LinkedIn observation, remains a separate user-gated track.
- Feature `272` operational routing program is a larger related program. This feature is the missing golden-path slice, not a rewrite of `272`.
- Swarm remainder, plugin runtime, launch-gate epics, and career copy are out of scope.
- Secrets never belong in explanations, receipts, or demonstrations.
