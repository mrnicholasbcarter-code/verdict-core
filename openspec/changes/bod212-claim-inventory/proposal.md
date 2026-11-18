# Proposal: BOD-212 public claim inventory

## Why

Verdict makes public capability claims in README.md, docs/*.md, and pyproject.toml.
Today there is no machine-checkable inventory linking each claim to its evidence,
and no gate preventing unlabeled or unevidenced claims from appearing in public docs.

## What Changes

- New file `docs/CLAIMS.md` lists every public claim with:
  - unique ID (C-XXX)
  - short quote of the claim
  - source file and line
  - label: `shipped` | `observed` | `proposed` | `superseded` | `disputed`
  - evidence path(s) that exist in the repo
- README.md and docs/*.md claims are marked with HTML comment markers:
  `<!-- claim:C-XXX -->` after each claim sentence
- New script `scripts/check_claims.py` enforces:
  1. Every README claim marker has a row in docs/CLAIMS.md
  2. Every CLAIMS.md row's evidence path exists in the tree
  3. Every claim has a label
  4. Proposed/disputed/superseded claims are not presented as shipped
- New test `tests/test_check_claims.py` runs the script in CI
- CI lint job runs `python scripts/check_claims.py` as a required gate

## Capabilities

### New Capabilities

- `claim-inventory`: A machine-checked registry of every public capability claim with its evidence and label.

### Modified Capabilities

- README.md and docs/*.md: claim markers added inline
- CI lint: new required check

## Impact

- `scripts/check_claims.py` — new verification script
- `tests/test_check_claims.py` — new test
- `docs/CLAIMS.md` — new inventory file
- `.github/workflows/ci.yml` — new lint step
- README.md, docs/architecture.md, docs/CONFIGURATION.md, docs/CAPABILITY_PASSPORTS.md, docs/BOD-333-AUTODEV-CONTEXT.md, docs/REPO-METADATA.md, docs/THREAT_MODEL_RECEIPTS.md, docs/credentials.md, docs/typescript-contract-parity.md, docs/USER_JOURNEY.md, pyproject.toml — claim markers added

## Evidence boundary

KNOWN: 108 claims catalogued across README.md, top-level docs/*.md, and pyproject.toml.
KNOWN: 1 claim is `observed` (C-016, dogfood live run), 4 are `proposed` (C-031, C-048, C-077, C-107), 1 is `verified` (C-108, proof matrix), 102 are `shipped` (source code + tests).
KNOWN: all evidence paths exist in the repo (validated by check_claims.py).
INFERRED: the CI gate will prevent future unlabeled or unevidenced claims.
NOT AVAILABLE: independent audit of claim accuracy beyond evidence existence.

## Acceptance criteria

- `docs/CLAIMS.md` exists and lists each claim with id, quote, file:line, label, evidence
- Every README/docs claim has a `<!-- claim:C-XXX -->` marker
- `python scripts/check_claims.py` exits 0
- `pytest -q tests/test_check_claims.py` passes
- CI lint runs `check_claims.py` and fails on missing markers, missing evidence, or unlabeled claims
- OpenSpec change validates strictly with zero open tasks