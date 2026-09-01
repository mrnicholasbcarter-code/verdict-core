# Contract: acceptance-gates report

**Owner**: `scripts/generate_gates_report.py` (produces) and `scripts/verify_gates.py` (validates)
**Consumer**: release reviewers, via `gates_status.json` in the evidence bundle

## Existing mechanism this feature extends

Gate statuses are computed by the generator; the verifier only validates the report's shape,
canonical JSON form, and evidence paths. **A new check is a change to the generator.**

A check counts as enforcing only if it is not advisory:

```
_is_advisory(step) == ("continue-on-error" in step) or ("|| true" in step)
```

Advisory detection is per workflow step, split on `^\s*- (name|uses):`.

## G5.3 needle table

Today:

| Requirement | Needle |
|---|---|
| python dependency audit | `pip-audit` |
| npm dependency audit | `npm audit` |
| osv scanner | `osv-scanner` |
| static analysis | `codeql` |

Semantics: needle missing → `BLOCKED`; every occurrence advisory → `FAIL`; otherwise `PASS`.

New scan classes are added as rows in this table. They must not be given a parallel
mechanism, because the report is the single artifact a reviewer reads.

Rows this feature adds:

| Requirement | Nature of the needle |
|---|---|
| bill of materials | The generator step that emits the component inventory |
| secret scanning | The history-scanning step, distinct from the existing committed-file check |
| pinned-revision check | The step asserting every third-party reference is immutable |
| dynamic verification | The step exercising the running server surface |

## G5.1 and G5.2 evidence resolution

```
_resolve_artifacts(gate, evidence_dir) -> path = evidence_dir / artifact
```

Symlinks are rejected by an explicit `is_symlink` guard. A missing artifact yields
`BLOCKED`.

`Gate("G5.1", …, artifacts=("THREAT_MODEL.md",))` and `Gate("G5.2", …,
artifacts=("PRIVACY_POLICY.md",))` therefore require **real copies inside the evidence
directory**. Authoring the documents at the repository root does not move these gates off
`BLOCKED` on its own.

`.github/workflows/acceptance-gates.yml` begins with `rm -rf evidence evidence_bundle`, so
the copy must be a step in that workflow, placed with the other evidence-generation steps
and before the non-advisory generator step.

`.md` is already in the evidence bundle's allowed suffixes, so no bundle change is needed.

## Report contract

`gates_status.json` keeps its existing shape. The statuses this feature is expected to move:

| Gate | Before | After |
|---|---|---|
| G5.1 threat model published | `BLOCKED` | `PASS` |
| G5.2 privacy policy published | `BLOCKED` | `PASS` |
| G5.3 supply-chain scans | `PASS` (4 needles) | `PASS` (extended needle set) |

A gate whose evidence is genuinely unavailable is reported as `BLOCKED` or `UNAVAILABLE`.
Constitution quality gate 6 forbids reporting an unknown as a pass, and this report is the
place that rule is enforced in practice.
