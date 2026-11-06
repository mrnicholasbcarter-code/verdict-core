# The prime-sync units install from the package

## Why
On the operator host Verdict is installed as a regular, non-editable package in
`~/dev/verdict-core/.venv/lib/python3.13/site-packages`. There the doctor repair
for a missing `verdict-prime-sync.timer` was:

`cp /home/nick/dev/verdict-core/.venv/lib/python3.13/site-packages/deploy/systemd/verdict-prime-sync.{service,timer} ~/.config/systemd/user/ && ...`

`ls` on that directory gives `No such file or directory`. The repair was built
from `verdict.ocr_release.package_root()`, the directory above the package.
That is `site-packages` in an installed wheel, and `deploy/` is not shipped, so
the repair failed on every installed host. The tests only ran from a checkout.
The template's own `ExecStart` also needed a checkout:
`$VERDICT_CHECKOUT/.venv/bin/python`, with
`VERDICT_CHECKOUT=%h/dev/verdict-core`.

## What Changes
- New `verdict/prime_sync_service.py` `manage_service()` renders both units from
  code. It mirrors `verdict/acceptance_service.py` `manage_service()`: the same
  `plan_units` / `save_rollback` / `_systemctl` helpers, the installing
  interpreter in `ExecStart`, and `WorkingDirectory` set to the directory that
  holds the package. The cadence is the old template's: `OnBootSec=2min`,
  `OnUnitActiveSec=15min`, `Persistent=true`. One addition to the mirrored
  pattern: a replaced timer is restarted, because `enable --now` leaves a
  running timer on its old schedule.
- New command `verdict harness prime install-service [--dry-run] [--uninstall]`,
  through the mutation action `harness.prime.install-service`, as
  `verdict acceptance install-service` works.
- The doctor repair for a missing timer is now
  `verdict harness prime install-service`. The inactive and failed repairs are
  unchanged. The doctor no longer imports `package_root`.
- `deploy/systemd/verdict-prime-sync.{service,timer}` are removed, so the units
  have one source.
- Docs: `docs/guides/harnesses.md`, `docs/CLI_REFERENCE.md`, `CHANGELOG.md`.

## Design choice
Rendering from code was chosen over shipping the template as package data.
Setup and the doctor already install the acceptance, identify and metadata
timers by rendering them from code with the running interpreter. A shipped
template would still carry `$VERDICT_CHECKOUT/.venv/bin/python`, which is wrong
on a host with no checkout, and it would be a second copy to keep in step.

## Capabilities
### Modified Capabilities
- `setup-host-prerequisites`: "Doctor reports each host prerequisite with its
  exact repair". The prime-sync scenario now names
  `verdict harness prime install-service`.
- Added to the same capability: "The prime-sync timer installs from the
  installed package".

## Impact
- Code: `verdict/prime_sync_service.py` (new), `verdict/doctor_diagnostics.py`,
  `verdict/actions/extra.py`, `verdict/actions/registry.py`,
  `verdict/commands/parsers_harness.py`, `verdict/commands/dispatch.py`.
- Tests: `tests/test_prime_sync_service.py` (new; it builds the wheel, extracts
  it into a bare `site-packages` and runs the repair, a dry run, an install and
  an uninstall from it with a temporary HOME and a `systemctl` on PATH that
  only logs its arguments), `tests/test_doctor_host_prerequisites.py`.
- The requirement being modified is added by the active change
  `bod219-setup-host-prerequisites`, which must be archived first. That change's
  proposal, task 6.3 and scenario still name the `cp` repair until this
  MODIFIED delta replaces it at archive.
- Not changed: `verdict setup` still has no prime-sync action. `verdict/setup_host.py`
  is owned by the ocr-pin change in flight. Adding `host.prime-sync-timer` on
  top of `prime_sync_service.manage_service` is follow-up work.
- Not changed: on a host whose live unit was copied by hand from the old
  template, nothing is rewritten until someone runs `install-service`.
