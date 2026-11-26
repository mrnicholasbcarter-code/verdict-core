# Spec Delta

## ADDED Requirements

### Requirement: Integrate and review nodes are merge-only

A work node of kind `integrate` or `review` SHALL NOT have `owned_files`. Constructing one that does,
and parsing a planner graph that holds one, SHALL fail with an error that names the node and its
kind, lists the files, and says to move the authoring work and those files to an implement node the
node depends on. The planning prompt SHALL state that an integrate node owns no files, writes nothing,
and may name in its verification command only test files that already exist or that an implement node
creates. The planner's repair call SHALL receive the error text. A repaired graph that still breaks
the rule SHALL fail planning closed (after the one repair round, before any worker runs). The runtime
SHALL keep not dispatching a worker for these nodes. `WorkGraph.from_dict` SHALL apply the rule by
default, so a graph supplied to `verdict orchestrate --graph` that breaks it SHALL be refused with
exit 2 before any run starts. A reader of a recorded run (`graph.json` for resume, controls and
receipts) SHALL opt out explicitly with `allow_merge_only_owned_files=True`; such a graph SHALL load
unchanged, with its digest, so a run recorded before this rule still resumes and its receipt still
verifies.

#### Scenario: an integrate node that owns files is refused

- **WHEN** a planner graph has an integrate node with `owned_files`
- **THEN** parsing fails with an error that names the node, its kind and its files and says to use an
  implement node

#### Scenario: the repair call acts on the reason

- **WHEN** the first planner output breaks the rule and the second does not
- **THEN** the repair prompt contains the error text, and the repaired graph, whose integrate node
  owns no files, is accepted

#### Scenario: a repair that keeps the rule broken fails closed

- **WHEN** both planner outputs give an integrate node `owned_files`
- **THEN** planning fails with "plan invalid after one repair round" carrying the reason, after two
  planner calls and before any worker call

#### Scenario: a file-less integrate or review node is accepted

- **WHEN** an integrate or review node has no `owned_files`
- **THEN** it is accepted as before

#### Scenario: a fresh graph is held to the rule

- **WHEN** `verdict orchestrate --graph FILE` is given a graph whose integrate or review node has
  `owned_files`
- **THEN** it prints `BLOCKED` with the node's error, exits 2, and creates no run directory

#### Scenario: a recorded graph still loads

- **WHEN** `WorkGraph.from_dict(..., allow_merge_only_owned_files=True)` reads a `graph.json` whose
  integrate node has `owned_files`, as resume, run controls and the receipt builder do
- **THEN** the graph loads with those files and the same digest, and the run resumes

### Requirement: Collection errors in files the node does not own are an environment error

`classify_verify_env_error` SHALL take the verified node's `owned_files`. When it is given them, a
pytest run (`pytest`, `py.test` or `<python> -m pytest`) that exits with code 2, prints
`N errors during collection`, and whose erroring paths (`ERROR <path>` lines and
`ERROR collecting <path>` headers) are all outside `owned_files` SHALL be an `environment_error`
whose reason names the command and the first three erroring paths. The runtime SHALL pass the node's
`owned_files` when it classifies a failed verification, so such a node takes the existing
`environment_error` path: one attempt, BLOCKED, no escalation count, no reassignment, no cooldown, and
no session ledger or health-cache entry. Without `owned_files` the rule does not apply.

The erroring paths SHALL be complete and repo-relative, or the failure SHALL stay
`verification_failed`: an `ERROR` line that holds anything but a plain repository path (for example a
path with a space) makes the list unreadable; a path that is absolute or has a `..` part cannot be
matched to an owned file without a repository root, so it makes the list inconclusive; and an output
of at least `VERIFY_OUTPUT_TAIL_CHARS` characters (the most the runner keeps, so its first lines may
be cut) that shows fewer erroring paths than the `N errors during collection` count makes it possibly
incomplete.

#### Scenario: collection errors only in unowned files

- **WHEN** a node verifies with `python -m pytest -q tests -k deliver`, pytest exits 2 with 59 errors
  during collection, and none of the erroring files is owned by the node
- **THEN** the failure category is `environment_error`, the node is BLOCKED after one attempt with
  no reassignment, cooldown or escalation count, and the reason names the command and the first paths

#### Scenario: a collection error in an owned file stays a verification failure

- **WHEN** at least one erroring path is a file the node owns, or lies under an owned directory
- **THEN** the failure category is `verification_failed` and the worker gets another attempt

#### Scenario: other pytest outcomes stay verification failures

- **WHEN** pytest exits 4 (a named test file is missing), exits 5 (no tests), exits 1 on test failures,
  exits 2 without a collection marker, or exits 2 on collection errors whose paths cannot be read
  from the output
- **THEN** the failure category is `verification_failed`

#### Scenario: a path that cannot be matched to an owned file is not unowned

- **WHEN** pytest reports a collection error at `/repo/tests/test_a.py` or `../repo/tests/test_a.py`
  and the node owns `tests/test_a.py`
- **THEN** the failure category is `verification_failed`, so the worker may correct its own file

#### Scenario: an erroring list that may be incomplete is not trusted

- **WHEN** an `ERROR` line holds a path with a space, or the output is at the runner's length cap and
  names fewer erroring files than pytest counted, and no path shown is owned
- **THEN** the failure category is `verification_failed`; a capped output that names every counted
  file is decided on its paths

#### Scenario: other commands and the older rules are unchanged

- **WHEN** the command is not pytest, or the exit code is 126 or 127, or `python -m MODULE` cannot
  find `MODULE`
- **THEN** the collection rule does not apply and the earlier environment rules apply as before
