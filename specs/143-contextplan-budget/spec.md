# BOD-143 ContextPlan pre-hydration estimates

## Outcome

Each eligible shortlist candidate gets a bounded context/tool/output budget
estimate before final selection. Task-fit / BOD-104 compare those estimates.
Only the selected model gets final hydration compile. A large context window
does not mean fill the window.

## Requirements

1. Final compiled pack respects the selected model budget.
2. Larger model context does not cause useless context inflation.
3. Pre-hydration plan influences model selection via assistance/expected cost.
4. Exact selected model influences post-selection context compilation.
5. Small task -> small focused pack; cross-module/coding task -> broader bounded pack.

## Constraints

- BOD-104 remains strategy authority.
- Do not invent quality from model names.
- Do not implement BOD-144 unified receipt beyond attaching plan digests on existing receipts.
