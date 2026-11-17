# M-1: stop false done

## Why
Stories were called done on fixture tests, green CI and merges while the real
product did not work on the operator host (failure review 2026-10-13, ROOT-1).
The definition of done, OpenSpec state, reachability debt and proof bundles
were advice, not gates.

## What Changes
- `scripts/check_dod.py` and `proof/dod.schema.json` make the seven-part
  definition of done executable from `docs/proof/dod/<story>.json`.
- The required `lint` job runs pinned `openspec validate --all --strict` and
  `scripts/check_openspec_tasks.py`; five invalid changes get spec deltas.
- `scripts/check_reachability.py` adds orphan modules, forbidden symbols and a
  shrink-only ratchet.
- `scripts/demo_orchestrate.py` requires `--out`; `scripts/check_proof_paths.py`
  guards `docs/proof/**`.
- The `lint` job applies ruff fixes itself on same-repository pull requests.
- `scripts/drill_gates.py` and `gate-drills.yml` re-prove every gate daily.

## Capabilities
### New Capabilities
None. This change is repository tooling and CI (`skip_specs: true`).
### Modified Capabilities
None.

## Impact
CI workflows, scripts, AGENTS.md and CONTRIBUTING.md. No product code under
`verdict/` changes. Pull requests now fail on new unreachable code, invalid or
unfinished OpenSpec changes and undeclared proof edits.
