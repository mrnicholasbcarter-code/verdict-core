# Design

## Context

`_apply_lint_autofix` builds a `targets` list of owned changed files and passes
all of them to `ruff check --fix` and `ruff format`. When a file is named
explicitly on the ruff command line, ruff bypasses its own `include`/`exclude`
configuration and processes the file as Python syntax regardless of its
extension. This corrupted a live YAML file: `schema: verdict-change-v1` became
`schema: verdict - change - v1` after `ruff format`.

## Goals / Non-Goals

**Goals:**
- Restrict the files passed to ruff to `.py` and `.pyi` only.
- Record skipped non-Python owned files in the `lint_autofix` event.
- Keep all other autofix behaviour (gate-conditional, owned-only, fail-safe
  ruff_missing, skipped when no ruff gate) unchanged.

**Non-Goals:**
- Ask ruff which files it would include via config (bypassed by explicit
  naming anyway).
- Run any other tool on non-Python files.

## Decisions

1. **Extension filter, not config inspection.** Ruff's `include`/`exclude` globs
   govern directory traversal; they do not apply when files are named explicitly.
   An extension-based filter (`{".py", ".pyi"}`) is simpler, stable across ruff
   versions, and correct regardless of config.

2. **Record skipped files in the event.** Non-Python owned files are not silently
   dropped; `skipped_non_python` in the `lint_autofix` event makes the filtering
   visible in the receipt.

3. **Empty `python_targets` returns early without calling ruff.** If a node owns
   only non-Python files (e.g. a spec-writing node), ruff is not invoked at all.

## Risks / Trade-offs

- **Python files are still auto-fixed; non-Python files are not.** This is the
  correct behaviour: ruff is a Python linter, not a general formatter.
- **The filter is static.** If ruff adds support for a new Python-compatible
  extension in the future, `_RUFF_PYTHON_EXTENSIONS` must be updated manually.
  This is a deliberate conservative choice.

## Security / trust implications

- The extension filter narrows the attack surface: even if an adversarial worker
  writes a file with a non-Python extension to an owned path, that file will not
  be passed to ruff.
- No new trust boundaries are introduced.

## Routing / context / memory implications

- No effect on route selection, admission, ranking, context packs, or memory.

## Migration / rollback

- No data migration. Roll back by removing the `python_targets`/`non_python_targets`
  split and reverting to the flat `targets` list.
- Existing receipts are unaffected; `skipped_non_python` is a new optional field.

## ADR impact

- No ADR changes required. This is a bug fix within the existing
  `lint-autofix-before-gates` feature boundary.
