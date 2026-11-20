# Spec Delta

## Purpose

Narrow the file scope of `_apply_lint_autofix` (introduced in
`lint-autofix-before-gates`) to Python source files only.  Passing non-Python
files to ruff by explicit path causes ruff to process them as Python syntax,
corrupting YAML, Markdown, TOML and other formats.

## MODIFIED Requirements

### Requirement: Autofix targets only Python source files

`_apply_lint_autofix` SHALL pass to ruff only files whose suffix is in
`{".py", ".pyi"}`.  Owned changed files with any other extension SHALL NOT be
passed to `ruff check --fix` or `ruff format`.

This modifies the existing requirement from `lint-autofix-before-gates`
("Only owned files that exist in the worktree are touched") by adding an
extension constraint.  All other conditions (owned, exists, not symlink, under
worktree root) still apply.

#### Scenario: Node owns .py, .yaml, and .md — only .py reaches ruff

- **WHEN** an IMPLEMENT node's `changed` list includes `a.py`, `config.yaml`, and `docs.md`
- **AND** all three are in the node's `owned_files`
- **THEN** only `a.py` is passed to `ruff check --fix` and `ruff format`
- **AND** `config.yaml` is not in any ruff invocation's argv
- **AND** `config.yaml` bytes are identical before and after `_apply_lint_autofix`
- **AND** the `lint_autofix` event has `skipped_non_python=["config.yaml", "docs.md"]`

#### Scenario: All owned changed files are non-Python — ruff is not called

- **WHEN** every owned changed file has a non-Python extension (e.g. `.yaml`, `.md`)
- **THEN** ruff is not called at all
- **AND** the `lint_autofix` event has `files=[]`, `changed_files=[]`,
  `skipped=False`, and `skipped_non_python=[<all non-python files>]`

## ADDED Requirements

### Requirement: lint_autofix event carries skipped_non_python field

Every `lint_autofix` event emitted after the `python_targets` / `non_python_targets`
split SHALL include a `skipped_non_python` field containing the list of owned
changed files that were excluded from ruff invocations because their extension
is not `.py` or `.pyi`.  An empty list is emitted when all owned changed files
are Python files.

Events emitted before the split (the `no_ruff_gate` skipped path and the
`no changed files at all` path) are exempt and do not carry `skipped_non_python`.

#### Scenario: Mixed Python and non-Python owned files — skipped_non_python recorded

- **WHEN** an IMPLEMENT node's changed list includes `a.py` and `config.yaml`
- **AND** both are owned by the node
- **THEN** the `lint_autofix` event has `skipped_non_python=["config.yaml"]`
- **AND** `a.py` is in the `files` field of the event
