# Tasks

## 1. Key resolution
- [x] 1.1 One resolver with env > credentials.env > key file > Prime auth.json precedence.
- [x] 1.2 Replace the direct `getenv("OMNIROUTE_API_KEY")` reads with it.
- [x] 1.3 Test that `harness prime sync-models` uses the key file when no env key is set.

## 2. Units
- [x] 2.1 Unit registry for the prober (agentic budgets) and Prime sync; no secret in units.
- [x] 2.2 `verdict setup services`, `verdict setup --check`, wizard warm-cache step.
- [x] 2.3 Remove `prove_at_rest_service.py` and the `deploy/systemd` templates.

## 3. Checks
- [x] 3.1 Doctor services section: per-unit state, last error line, cache age and usable routes.
- [x] 3.2 Doctor stale-list check: proven FREE routes outside cooled pools > 0 and FREE eligible == 0 is an issue.
- [x] 3.3 Prober daemon prints one summary line per finished cycle.
- [x] 3.4 Tests: stopped timer, failed unit, missing unit, stale cache, no unit-readable key.

## 4. Live
- [x] 4.1 Install both units on the operator host with `verdict setup services` (evidence.md).
- [x] 4.2 Stop the prober timer; doctor and `setup --check` exit 1 and name the unit (evidence.md).
- [ ] 4.3 Merged-main run and the parent's own re-run (definition of done 1, 6, 7).
