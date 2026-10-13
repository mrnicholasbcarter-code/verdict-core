# Verdict user services are installed, keyed and checked

## Why
Verdict's background services failed four times without anyone noticing.
The prober (`verdict-prove-at-rest`) was never installed, so the health cache
emptied and eligibility, `serve` and free-first selection failed closed. The
Prime model sync timer failed every 15 minutes with HTTP 401 for days: its unit
had no gateway key, because Verdict only read the key from the exported shell
environment. Nothing reported a failed user unit.

## What Changes
- One gateway key resolver (`credentials_store.resolve_gateway_key`):
  `VERDICT_OMNIROUTE_API_KEY` > `OMNIROUTE_API_KEY` from the environment, then
  the same names in `credentials.env`, then the owner-only
  `~/.config/verdict/omniroute-api-key` file, then the `omniroute` key in
  Prime's `auth.json`. Files that group or others can read are skipped. The
  seven direct `getenv("OMNIROUTE_API_KEY")` reads now call it.
- One unit registry (`verdict/user_services.py`) replaces
  `prove_at_rest_service.py` and the hand-copied `deploy/systemd` templates. It
  renders the prober (with the agentic budget flags) and, when Prime is
  installed, the Prime sync timer. Units hold no secret. Install refuses when
  no key source is readable without the shell environment.
- `verdict setup services` installs every unit; `verdict setup --check` exits
  1 when a unit is missing, inactive or failed. The setup wizard's warm-cache
  step installs the units instead of printing "not available yet".
- `verdict doctor` reports each unit (installed, active, last result, last
  error line) and the health cache (age of the newest probe, fresh + stale),
  and exits 1 when one is wrong. Doctor is the check; no OnFailure notifier is
  added (the hourly acceptance job owns notification).

## Impact
Probing, ranking and admission are unchanged. Exported environment keys still
win over every file. No gateway configuration or credential is written.

## Evidence boundary
KNOWN: focused offline tests with a fake systemd runner pass on the story branch.
The live install and doctor drill on the operator host are recorded in
`evidence.md` after the run.
