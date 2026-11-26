# Spec Delta: repository-tooling

Every scenario below has one test in `tests/test_check_dod.py`, named `test_` plus the scenario title in
snake case.

## ADDED Requirements

### Requirement: check_dod SHALL offer a draft mode that skips only the confirmation and the archive

`scripts/check_dod.py --draft --evidence-root <dir> [--root <repo>] <file>` SHALL check an evidence file as
the strict mode does, except for two things: `parent_confirmation` need not be present, and the OpenSpec
change need not be archived. It SHALL still apply the schema (with `parent_confirmation` the only field
no longer required), the hash, location, redaction and marker checks of every raw output, the ancestry
checks, the freshness window, the exit-code and expected-text checks, the structured-result checks and
the `not_applicable` checks. A `parent_confirmation` that is present SHALL be checked as in strict mode.
Raw outputs SHALL be read from `<dir>` (their recorded `docs/proof/dod/...` paths resolved under it) while
every git check runs in `--root`. A draft SHALL never print `DONE` or `NOT DONE`; it prints `DRAFT OK`
with the names of the pending checks, or `DRAFT FAILED` with each error, and exits 0 or 1. `--draft`
SHALL be refused with `--all`, without `--evidence-root`, and when `<dir>` is inside `--root`.
`--evidence-root` without `--draft` SHALL be refused. Strict mode and `--all` SHALL behave exactly as
before, and CI SHALL NOT use `--draft`.

#### Scenario: Draft without a confirmation passes when everything else holds
- **WHEN** a complete evidence file lacks only `parent_confirmation` and its change is not archived
- **THEN** `--draft` prints `DRAFT OK` naming both pending checks and exits 0

#### Scenario: Draft never prints DONE
- **WHEN** `--draft` passes or fails
- **THEN** the output contains neither `DONE` nor `NOT DONE`

#### Scenario: Draft with a missing confirmation does not return early
- **WHEN** a draft lacks `parent_confirmation` and also has a raw output whose digest does not match
- **THEN** the digest error is reported, not only the missing field

#### Scenario: Draft still rejects a wrong digest
- **WHEN** a raw output differs from its recorded SHA-256
- **THEN** `--draft` fails naming the output

#### Scenario: Draft still rejects an ancestry violation
- **WHEN** a run's `sha` is not a descendant of `merged_sha`, or is not an ancestor of the repository's `HEAD`
- **THEN** `--draft` fails naming the run

#### Scenario: Draft still rejects a fresh run inside the window
- **WHEN** the fresh run is less than `freshness_window_hours` after the first
- **THEN** `--draft` fails with `not after the freshness window`

#### Scenario: Draft still rejects an unredacted output
- **WHEN** a raw output holds text that `redact_text` would change
- **THEN** `--draft` fails naming the output

#### Scenario: Draft still checks a confirmation that is present
- **WHEN** a draft carries a `parent_confirmation` whose `by` equals `author`
- **THEN** `--draft` fails with the author-cannot-confirm error

#### Scenario: Draft reads outputs from the evidence root and git from the repository root
- **WHEN** outputs exist only under `<dir>/docs/proof/dod/` and the commits only in `--root`
- **THEN** `--draft` passes, and an output that exists only under `--root` is reported missing

#### Scenario: Draft is refused in the wrong shapes
- **WHEN** `--draft` is given with `--all`, without `--evidence-root`, or with an evidence root inside `--root`
- **THEN** the call exits 2 naming the rule and checks nothing

#### Scenario: CI does not use draft mode
- **WHEN** `.github/workflows/ci.yml` and `scripts/check_lint_gates.py` are read
- **THEN** neither contains `--draft`

#### Scenario: Strict mode is unchanged
- **WHEN** the same file lacking `parent_confirmation` is checked without `--draft`, and `--evidence-root` is given without `--draft`
- **THEN** the first fails on the missing field with the exit and text of today, and the second exits 2
