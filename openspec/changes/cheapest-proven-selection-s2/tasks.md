## Implementation and local proof
- [x] Reproduce serve tier ordering, billing, read-only choose and task-family defects. Baseline: 7 failed, 10 passed. Diversity regression: 1 failed, 14 passed. Same-fixture protected regression: 1 failed.
- [x] Implement shared capacity classification and snapshot evidence, ordinary-task tier-first Top-k, route-scoped 402 feedback, read-only choose, and text-family rating. WORTHY tasks retain existing proof-bar ranking; two protected security tests are unchanged. Existing admission and permit constants remain unchanged.
- [x] Run every affected symbol/module-reference test file. Final affected suite: 2145 passed, 13 skipped, 4 xfailed in 161.92 s (105 files). One ordinary coding assertion migrates higher-score paid winner to qualified-free winner; strong-admitted and confirm==[] assertions remain.
- [x] Run full suite with 4 workers / loadfile under 4G and 1800 s. First run: 3 failed, 10611 passed, 31 skipped, 24 xfailed. Parent-approved protected ranking correction and ordinary assertion migration followed. Confirming run: 10615 passed, 31 skipped, 24 xfailed, 40 warnings in 233.64s (0:03:53).
- [x] Run static and OpenSpec checks: ruff clean, 1225 files formatted; strict mypy 325 source files clean; reachability 2351 symbols / 0 NEW and 13 orphan modules / 0 NEW; doc links 411 files; proof matrix 20 rows / claims ledger 10 entries; strict OpenSpec validation valid.

Post-merge host proof is listed in the proposal rollout. This task list records local evidence only, not merged-main, installed-host acceptance or Done.
