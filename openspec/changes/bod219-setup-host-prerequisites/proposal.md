# BOD-219 part 2: setup installs, and doctor reports, the host prerequisites

## Why
Restoring the operator host on 2026-11-05 needed six steps by hand: the pinned
`ocr` reviewer, a first Core metadata refresh and its timer, the `verdict serve`
user unit with an env file, a Prime registry with `providers.omniroute`, and the
acceptance and identify timers. `verdict setup` did none of them, and
`verdict doctor` either said nothing (ocr, serve, Prime registry, acceptance
timer) or named no exact repair. The definition of done requires that every
service or timer a story needs is installed by `verdict setup` and checked by
`verdict doctor`.

## What Changes
- `verdict/ocr_release.py` is the one ocr helper. It reads `OCR_VERSION` and
  `OCR_SHA256` from `.github/workflows/certify-rehearsal.yml` (the single source
  of truth), classifies a binary as `ok`, `missing`, `wrong_version` or
  `digest_mismatch` (digest first, so the pinned binary is never executed), and
  installs the pinned asset at `~/.local/bin/ocr` only after the downloaded
  bytes hash to the pin. `scripts/check_dev_readiness.py` uses the same pin and
  classification; its output is unchanged.
- `verdict/serve_service.py` follows the existing installer pattern (render,
  dry-run without writes or systemctl, rollback copies, `enable --now`) for
  `~/.config/systemd/user/verdict-serve.service`: `127.0.0.1:8000`,
  `WorkingDirectory=%h`, `EnvironmentFile=%h/.config/verdict/serve.env`. The env
  file is 0600 and holds `LLMGATE_AUTH_TOKEN` and `VERDICT_RECEIPTS_DB`. The
  token is reused (exported, credential store, then the current env file) or
  generated with `secrets.token_urlsafe` and stored in the credential store.
  Token values never appear in results, errors or logs; a token the file cannot
  carry fails closed before any write.
- `harness_prime.create_omniroute_registry` seeds a missing `models.json` with
  `providers.omniroute` (`baseUrl` = gateway + `/v1`, `api:
  openai-completions`, `apiKey` = the env var name `OMNIROUTE_API_KEY`) and never
  replaces a file. `omniroute_registry_state` gives doctor a read-only view.
- `verdict/setup_host.py` plans one consent-required capability-bootstrap action
  per unsatisfied prerequisite (`host.ocr`, `host.metadata`,
  `host.acceptance-timer`, `host.identify-timers`, `host.serve`,
  `host.prime-registry`) and applies it with the existing installers:
  `ocr_release`, `metadata_service`/`acceptance_service`/`identify_service`
  `manage_service`, `serve_service`, and `harness_prime` `sync_models` (through
  the `harness.prime.sync-models` action) and `enable`. Metadata refresh runs only
  when the store is missing. Prime enable waits up to 15 probes for serve's
  `/health`. A serve unit that is active but does not answer `/health`, or whose
  probe raises, is not satisfied: setup plans `host.serve` and APPLY restarts it, then
  fails with its logs if it is still unhealthy.
- `capability_bootstrap.run_bootstrap` accepts `host_planner`/`host_runner`. Host
  actions join all-scope plans only, pass the existing consent gate (`--yes`
  interactive, `--allow` ids under `--non-interactive`), are re-planned after
  drift instead of being skipped as already applied, and roll back as
  `manual_undo_required` with their uninstall command. `verdict setup
  --recommended` and `--apply` wire them in; the classic `--dry-run`/`--plan`
  preview never probes the host.
- `verdict doctor` adds a Host prerequisites section (ocr; serve unit and
  `/health`; Prime registry; a failing `verdict-prime-sync.service`) and an
  Acceptance timer row; the metadata, identify and prime-sync timer rows end with
  their repair. Each failed row is an issue and ends with `repair: <command>`,
  which `repair_command` returns before its keyword guesses. `verdict-serve.service`
  joins the installed-runtime provenance check.
- Docs: `docs/CLI_REFERENCE.md` (setup and doctor), `docs/guides/setup-preview.md`,
  `docs/guides/harnesses.md`.

## Capabilities
### New Capabilities
- `setup-host-prerequisites`: setup installs, and doctor reports with an exact
  repair, the host prerequisites a Verdict host needs.

## Impact
- New: `verdict/ocr_release.py`, `verdict/serve_service.py`, `verdict/setup_host.py`.
- Changed: `verdict/capability_bootstrap.py`, `verdict/cli.py` (setup wiring),
  `verdict/harness_prime.py`, `verdict/doctor_diagnostics.py`,
  `verdict/doctor_presentation.py`, `scripts/check_dev_readiness.py`.
- Tests: four new files; `tests/test_cli_inprocess.py` and
  `tests/test_doctor_shared_memory.py` stub the host rows like their existing
  systemd stubs, and one in-process doctor test is added.
- Not changed: the prime-sync timer still has no installer. Doctor names the
  documented install for a missing timer, `cp <checkout>/deploy/systemd/verdict-prime-sync.{service,timer} ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now verdict-prime-sync.timer`, as in
  `docs/guides/harnesses.md`; `enable --now` for an inactive one; and
  `verdict harness prime sync-models` for a failed one. A
  `models.json` that exists without `providers.omniroute` is reported, not edited.
- ADR disposition: no new decision; this reuses the bootstrap consent gate and the
  existing installers.
- Not claimed: a run on the operator host or from merged main.
