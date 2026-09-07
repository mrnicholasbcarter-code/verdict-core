# Validation Guide: Recruiter-Ready README and Proof Demo

This guide is prospective. Commands below are validation steps, not claims that evidence already exists. Record actual return codes, output, source revision, and limitations during implementation.

## 1. Establish the source state

Record the exact commit SHA and confirm the implementation worktree contains no unrelated generated changes. If the tree is dirty, bind evidence to an immutable snapshot including the diff rather than claiming commit-only provenance.

## 2. Run focused regression checks

Run the repository environment setup command documented by the project, followed by:

```bash
.venv/bin/python -m pytest -q tests/test_flagship_demo.py tests/test_documentation_smoke.py
.venv/bin/ruff check verdict/flagship_demo.py tests/test_flagship_demo.py tests/test_documentation_smoke.py
bash -n quickstart.sh
.venv/bin/python -m verdict quickstart --non-interactive --dry-run
git diff --check
```

Expected outcomes:

- the README output block exactly matches the canonical rendered fixture report;
- one route is selected and exactly three candidates have the specified reasons;
- the receipt is visibly marked `deterministic_fixture`;
- all changed local README paths and fragments resolve;
- no provider credentials, gateway, or application-state write are required.

Do not copy expected output into an evidence record until it has actually been observed from the final implementation revision.

## 3. Verify a fresh install without credentials

Create a disposable environment outside the repository, clear provider credential variables, install the final candidate artifact or exact revision using the repository's supported installation path, and run:

```bash
verdict quickstart --non-interactive --dry-run
```

Record the environment description, exact install source, commands, return codes, actual output, and whether any files were written in the empty working directory. A source-checkout invocation alone does not satisfy the fresh-install requirement.

## 4. Produce the terminal capture

Record the same final install-and-proof journey used in step 3. The recording or GIF must show the actual shipped command and output and must identify the revision it represents. Do not reconstruct frames, output, or elapsed timing. Confirm the capture contains no secrets, private paths, or unrelated terminal history before making it public.

## 5. Verify README links and fragments

Run the focused documentation test that parses README local links and heading fragments. Record zero unresolved local targets as a pass. Report external links separately; local path validation does not prove external availability.

## 6. Record the 90-second skim evidence

Automated checks may record:

- required section ordering;
- one install command and one proof command before output;
- presence of product, proof, limitation, and next-action links;
- exact output and link integrity.

A human reviewer must separately perform the timed skim and record whether they could identify product, install, proof, and next action. Until that occurs, mark the human criterion `not-run`; do not infer it from automated checks.

## 7. Run repository and PR gates

Run all repository-native required checks and confirm the authoritative PR state is bound to the exact reviewed head SHA. Record every required check name and conclusion, mergeability, and any unavailable or neutral status without translating it to success.

## 8. Closure decision

Issue #455 is ready to close only when focused regression, fresh-install transcript, exact terminal capture, link/fragment result, full required checks, and actual human skim evidence are all present and traceable to the final revision. Otherwise report the missing item as a blocker.
