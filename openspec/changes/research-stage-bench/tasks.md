# Tasks — research-stage bench (RS1)

- [ ] 0.1 Confirm FU1's factored harness parts (task set format, arm runner, baseline comparison) are on main; RS1 imports them.
- [ ] 1.1 Task set: pick about 6 nodes from real closed stories; commit the criteria and the list.
- [ ] 1.2 Stop rule: write the rule file (D18 shape) and commit it before any run.
- [ ] 2.1 Arm B: free read-only researchers with call and token caps; findings carry sources; uncited findings dropped.
- [ ] 2.2 Arm B synthesis: frontier route reads each cited source, drops failed citations, counts citation errors, writes the pack within the worker's budget.
- [ ] 2.3 Arm C: arm A extended with next-ranked retrieval to arm B's pack size (design.md, "Arm C padding"); `size_control_invalid` outside 5%.
- [ ] 2.4 `verdict bench research-pack`: requires consent, budget and `--stop-rule`; same worker route and budget across arms.
- [ ] 3.1 Report: the five metrics per arm, per-task detail, the stop-rule hash, the researcher caps; JSON and markdown.
- [ ] 4.1 Tests on fixtures (labelled fixtures): arm isolation, citation dropping, the size control, refusal without a stop rule, read-only tool grants.
- [ ] (post-merge) 5.1 Run the first ~6 tasks live; commit the report and raw receipts under `docs/proof/research-bench/` and index them.
- [ ] (post-merge) 5.2 Operator decision recorded: adopt, change or drop.
