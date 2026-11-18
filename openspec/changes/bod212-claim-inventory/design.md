# Design: BOD-212 public claim inventory

## Inventory file format

`docs/CLAIMS.md` is a Markdown table with columns:
| ID | Claim (short quote) | Source file:line | Label | Evidence |

- ID: `C-` + zero-padded 3-digit number (C-001, C-002, ...)
- Claim: short quote (≤120 chars) of the exact claim text
- Source: `file.md:line` or `file.py:line`
- Label: one of `shipped`, `observed`, `proposed`, `superseded`, `disputed`
- Evidence: comma-separated list of repo-relative paths that must exist

## Claim markers

In source documents (README.md, docs/*.md, pyproject.toml), each claim sentence
gets an HTML comment marker appended:
`<!-- claim:C-XXX -->`

Markers are placed at the end of the sentence, before the period if possible.
Multiple markers can appear on one line for compound sentences.

## Verification script

`scripts/check_claims.py` performs:

1. **README coverage**: extracts all `<!-- claim:C-XXX -->` markers from README.md
   and verifies each has a row in CLAIMS.md
2. **Evidence existence**: for each CLAIMS.md row, verifies every evidence path
   exists in the repo (file or directory)
3. **Label presence**: every CLAIMS.md row has a non-empty label
4. **Proposed/disputed gating**: warns about proposed/disputed/superseded claims
   but does not fail (they are allowed if labeled honestly)
5. **Summary**: prints claim count by label and total

Exit codes: 0 = all checks pass, 1 = any check fails.

## CI integration

Added to `.github/workflows/ci.yml` lint job as a required step after `check_dod.py`:
```yaml
- name: Claim inventory (every README claim labeled and evidenced)
  run: python scripts/check_claims.py
```

## Test

`tests/test_check_claims.py` runs the script via subprocess and asserts:
- exit code 0
- output contains "Claim Count by Label"
- output contains "Total claims:"
- output contains "All checks passed!"

## Labels definition

| Label | Meaning | Evidence requirement |
|-------|---------|---------------------|
| `shipped` | Implemented and tested in this repo | Source file + test that exercises it |
| `observed` | Verified in a live run with receipt | `docs/proof/...` receipt or proof bundle |
| `proposed` | Designed but not yet implemented | Design doc or spec |
| `superseded` | Replaced by a newer approach | Superseding doc or ADR |
| `disputed` | Accuracy challenged | Counter-evidence or open issue |

## Files modified

| File | Change |
|------|--------|
| `docs/CLAIMS.md` | New inventory table (108 rows) |
| `scripts/check_claims.py` | New verification script |
| `tests/test_check_claims.py` | New test |
| `.github/workflows/ci.yml` | New lint step |
| `README.md` | 29 claim markers added |
| `docs/architecture.md` | 18 claim markers added |
| `docs/CONFIGURATION.md` | 4 claim markers added |
| `docs/CAPABILITY_PASSPORTS.md` | 10 claim markers added |
| `docs/BOD-333-AUTODEV-CONTEXT.md` | 10 claim markers added |
| `docs/REPO-METADATA.md` | 2 claim markers added |
| `docs/THREAT_MODEL_RECEIPTS.md` | 5 claim markers added |
| `docs/credentials.md` | 11 claim markers added |
| `docs/typescript-contract-parity.md` | 6 claim markers added |
| `docs/USER_JOURNEY.md` | 12 claim markers added |
| `pyproject.toml` | 1 claim marker added |