# BOD-143 plan

1. Add deterministic `estimate_task_pack_need` + `plan_for_candidate` on ContextPlan.
2. After live shortlist, attach per-candidate ContextPlan estimates and update offer assistance tokens.
3. Pass selected plan token_budget into `build_cheap_path_context_pack`.
4. Tests for AC1-AC5 on live prepare/route path.
