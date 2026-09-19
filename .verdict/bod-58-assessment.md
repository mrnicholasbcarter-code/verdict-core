# BOD-58 Assessment — [CONTINUITY][E02] Paired context + routing quality/cost evaluation

File: .verdict/bod-58-assessment.md (this file)
Repo: verdict-core (workspace /home/nick/dev/verdict-core)
Status: READY but NOT implemented; independent of BOD-102/BOD-9 per Linear (status Ready, Todo); no supersession of BOD-104 (BOD-104 authority preserved — no second router introduced).

## Capability satisfied?
Not satisfied. No dedicated paired-evaluation file exists for BOD-58 (e.g., no `verdict/bod58_paired_evaluation.py`). Existing code covers pieces but does not combine them into a paired evaluation.

## Interfaces inspected (exact paths + line numbers)

- verdict/chooser.py
  - class ChooseReceipt (l.227)
  - def choose_route(...) (l.385)
  - def rank_admitted_candidates(...) (l.519)
  - def production_ranker(...) (l.173)
  - CandidateEvidence, resource_class_from_evidence (l.77)
  - choose_route depends on classifier.classify (imported l.20)

- verdict/planner.py
  - class StructuredPlanner (l.77)
  - def plan(...) (l.86)
  - def route(...) (l.207)
  - def estimate(...) (l.140)
  - def capability_requirements(...) (l.166)
  - PlannerPolicy (l.45), PlanResult (l.59), WorkflowKind (l.23)
  - Uses capacity floor / budget enforcement (l.420+)

- verdict/cost_ledger.py
  - class CostLedger (l.236)
  - def reserve(...) (l.315)
  - def reconcile(...) (l.394)
  - def remaining_cash_usd / remaining_cheaper_inference_usd (l.295-305)
  - CostTerm, Reservation, QuotaEvidenceInput (l.70+)

- verdict/live_routing.py
  - class RouteSelection (l.85), Candidate (l.76), Mix (l.65), UsageSnapshot (l.97)
  - def select_route(...) (l.283)
  - def explain(...) (l.322), def failover_order(...) (l.318)
  - Does NOT supersede BOD-104 router; it operates at a different layer (candidate selection vs gateway routing).

- verdict/capacity_models.py (referenced in task; inspected via file list; interfaces: capacity calculation / resource class mapping — not fully expanded here due to bounded scope; no conflicts found with BOD-58 pairing).

- verdict/live_routing_gateway.py (exists; gateway adapter; does not implement paired evaluation).

## Dependency relationships for BOD-58
- Needs chooser.py (choose_route / rank_admitted_candidates) for model/routing selection.
- Needs planner.py (plan / route / estimate / capability_requirements) for paired context/intake.
- Needs cost_ledger.py (CostLedger, reserve/reconcile, cost terms) for paired cost tracking.
- Needs live_routing.py (RouteSelection / Candidate / explain) for routing quality comparison.
- Must NOT introduce a second router that conflicts with BOD-104 authority (live routing gateway remains authoritative).

## Acceptance criteria remain (from Linear recoverable? partial — file-based recovery only; no Linear API in scope)
- Paired evaluation definition: what "context" and "routing" pairs mean for a given task class (implementation, protected, etc. per chooser.py task_class).
- Quality metric pairing: how routing quality is measured alongside context quality (not invented here).
- Cost pairing: how CostLedger reservation/reconcile pairs with routing selection; no synthetic benchmarks.
- Integration point: adapter/stub must call existing interfaces (choose_route, StructuredPlanner.plan/route, CostLedger) without duplicating gateway authority.

## Smallest correct change
Create a bounded adapter/stub file (`verdict/bod58_paired_adapter.py` or `.verdict/bod58-plan.md`) that:
1. Declares the pairing interface (context + routing) referencing the above line-numbered functions.
2. Documents the dependency map above.
3. Provides a stub `paired_evaluate(task_class, context, routing_selection)` that delegates to `choose_route` (chooser), `plan` (planner), and `CostLedger` (cost_ledger) without implementing synthetic evaluation logic.
4. Explicitly notes BOD-104 router authority is preserved.
This is the minimal non-breaking addition; full paired-evaluation logic should not be synthesized without Linear AC confirmation and real fixtures.

## Does it supersede existing code?
No. chooser.py handles model selection (BOD-95); planner.py handles planning; cost_ledger.py handles cost accounting; live_routing.py handles selection. BOD-58 is a cross-cutting evaluation layer, not a replacement. BOD-104 router authority remains intact.

## Recommendation
Do NOT invent a full paired-evaluation implementation. The bounded adapter/stub (`.verdict/bod-58-assessment.md` + `verdict/bod58_paired_adapter.py` stub if needed) satisfies assessment requirements without breaking architecture. Confirm Linear ACs before expanding logic.
