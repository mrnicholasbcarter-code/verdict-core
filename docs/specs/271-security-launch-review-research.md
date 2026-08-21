# V1-008 Research Notes

## Existing capability discovery

- `.github/workflows/security.yml` already owns Python dependency, Bandit,
  credential-file, npm, and OSV checks.
- `RELEASE_CHECKLIST.md` is the existing release acceptance surface.
- `tests/test_launch_gates.py` is the focused executable contract for this
  story.
- `SECURITY.md` and `docs/THREAT_MODEL_RECEIPTS.md` define the project's
  fail-closed, secret-free evidence boundary.

## Alternatives considered

- Reuse the existing workflow and checklist: selected.
- Add a new security scanner or runtime service: rejected; it would duplicate
  existing CI ownership and exceed the issue's scope.
- Record a launch approval locally without hosted evidence: rejected; issue
  acceptance explicitly requires evidence-bound review and CI state.

## Decision

EXTEND the existing workflow/checklist contracts and protect them with tests.
The prior vulnerability exceptions were removed because they made dependency
review advisory. Any current upstream vulnerability must remain visible as a
blocking limitation rather than being silently accepted.
