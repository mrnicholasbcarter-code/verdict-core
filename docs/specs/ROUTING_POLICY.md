# Routing Policy

**Status:** Active
**Authority:** This policy governs the Verdict Core routing surface. The former
LLM-gate name is retained only in historical migration material; changes require
an ADR.
**Related ADR:** `ADR-ORCHESTRATOR-ROUTING.md` — defines the orchestrator/gate boundary.

---

## 1. What Verdict IS (The Gate)

Verdict is a **thin, deterministic, fail-closed eligibility gate**. It does three things:

| Job | Component | Description |
|-----|-----------|-------------|
| **Eligibility filtering** | `EligibilityGate` | Consults `AvailabilityCache` + `ProbeRunner`; admits only `eligible`/`ready`/`degraded` candidates. Protected work **fails closed** when live truth is absent. |
| **Catalog mirror** | `GET /v1/models` | Serves the full live OmniRoute catalog (pricing, capabilities, ratings, context window) so the orchestrator can "review all models like openrouter.ai". |
| **Explainability** | `GET /v1/route/explain` | Returns full candidate set, pre-ranking eligible set, per-candidate exclusion reasons, cache confidence, refresh errors. |

**The gate is the enforcement layer. It has NO selection logic.**

---

## 2. What Verdict IS NOT (The Selector)

**Model selection is the orchestrator's job.** The orchestrator (the frontier model you pay for: `LLMGATE_PRIMARY`) performs the expensive cognitive pass **once per unit of work**:

1. Research task → review live catalog → pick right-sized model per slice
2. Spec → dispatch to workers (ruflo swarm / agent team / AgentSDK)
3. Verify worker output → confirm green
4. Feed outcome to learning loop (`record_outcome` → `ruflo hooks_model-outcome` → SONA/ReasoningBank)

**The gate does not:**
- Compute tiers via `classifier.py` regex (DEPRECATED for non-protected path)
- Rank candidates (`intelligence.route()` no longer calls `select_best_model`)
- Maintain hardcoded allowlists, tier rings, or worker pools
- Couple to Ruflo/RuVector **internals** for selection

---

## 3. The Dynamic-Catalog Rule (USER-EXPLICIT)

> **Do NOT hardcode worker allowlists or static tier rings.**
> Consume the dynamic model list returned by the gateway and assign the best-available model per task criticality.
> — ADR-149: drop the 3-tier (Haiku/Sonnet/Opus) abstraction, operate on concrete ModelId strings, pick the cheapest model predicted to clear a qualityBar.

**Pitfall:** `classifier.py`'s regex table is a hand-maintained static list — a brand-new capable slug silently lands in tier 2. This is the real "stale heuristic" problem, NOT a hardcoded 3-model allowlist (that misconception has caused wrong fixes before).

**Prefer:** OmniRoute `auto`/`auto/coding` server-side selection or ADR-149 per-ModelId cost-optimal selection over editing the regex.

---

## 4. Protected Work = Deterministic Frontier Floor

For **protected work** (money-level trading decisions, architecture, final synthesis):
- The orchestrator **may only** dispatch to workers the gate marks `ready`
- The gate enforces a **deterministic frontier floor** — no model below the safety floor is admitted
- The floor is derived from the live catalog's capability signal, NOT a hardcoded tier

For **non-protected work** (docs, refactors, analysis, bulk transforms):
- The orchestrator picks any eligible model from the live catalog
- Free/mid-tier models are preferred when appropriate
- The gate admits `eligible`/`ready`/`degraded` (with dev-mode unverified admission flag)

---

## 5. Reuse, Don't Reinvent

Verdict already has the primitives — use them:

| Primitive | Location | Use For |
|-----------|----------|---------|
| `ProbeRunner` + `openai_probe_transport` | `verdict/probes.py` | Live one-token probes (built, tested) |
| `AvailabilityCache` (singleton) | `verdict/availability_cache.py` | Bounded TTL + stale-while-revalidate |
| `OmniRouteAvailabilityAdapter` / `ProbeEnrichedAdapter` | `verdict/availability.py` | Catalog+runtime or catalog+runtime+probe truth |
| `SwarmDispatcher` | `verdict/dispatcher.py` | Filters `eligible` by `live_eligible`; records exclusions |
| ruflo intelligence hooks | CLI/MCP | `hooks_model-route`, `hooks_model-outcome` for learning loop |

**Verdict MUST NOT couple to Ruflo/RuVector internals.** Use the documented CLI/MCP surface, not internal imports.

---

## 6. The #57 / #72 / #73 Contract (Canonical Worked Example)

**Invariant:** Filter candidates before any ranking, and no ranker / Ruflo plan / RuVector result can reintroduce an excluded candidate.

1. Single `EligibilityGate` consults `AvailabilityCache.get(model_id)` — the ONLY authority used by router, dispatcher, Gate, and explain.
2. Protected work fails closed: when live availability truth is `unknown`/`error`/missing, exclude (do not optimistically admit). Dev/non-protected may admit unverified with a flag.
3. `/v1/route/explain` (#73) must surface the full candidate set, the pre-ranking eligible set, per-candidate exclusion reasons, and cache confidence/refresh_error — all from the same gate.
4. Integration tests cover every public route entry point (`/v1/route`, `/v1/chat/completions`, `Gate.route`, `intelligence.route`).

---

## 7. Verification (Run Before Claiming Done)

```bash
uv run --extra dev --extra dashboard --extra server ruff check . && uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
uv run pytest -q
code-review-graph detect-changes  # blast-radius on routing path
# Exact-SHA CI watch after push (CI/Lint/CodeQL must be green)
```

---

## 8. Anti-Patterns (Do Not Do)

| Anti-Pattern | Why It's Wrong | Correct Approach |
|--------------|----------------|------------------|
| Add `classifier.py` import to `intelligence.route()` | Puts selection in request path | Orchestrator selects; gate filters |
| Hardcode `model_allowlist = ["opus", "sonnet"]` | Violates dynamic-catalog rule | Derive from `GET /v1/models` |
| Store worker outcomes in Verdict's request path | Breaks deterministic enforcement | Subprocess `ruflo hooks_model-outcome` |
| Call `select_best_model(tier)` in hot path | Tier = stale heuristic | Orchestrator picks by live metadata |

---

## 9. For Contributors / Models Pointed At This Repo

- **Read `ADR-ORCHESTRATOR-ROUTING.md` before touching** `router.py`, `intelligence.py`, `gate.py`, `availability.py`, `availability_cache.py`, `probes.py`, or `api.py`.
- If you are about to add a hardcoded model name, tier, or allowlist — **stop.** Derive it from the live catalog instead.
- The gate (eligibility/probe/fail-closed) is sacred and deterministic. The *selector* lives in the orchestrator + learning loop, not in the request path.

---

*Policy aligned with `ADR-ORCHESTRATOR-ROUTING.md` and `ORCHESTRATOR_DRIVEN_ROUTING.md`. This document replaces the previous "no Ruflo coupling" absolute with "no selection logic in the request path; orchestrator owns selection."*
