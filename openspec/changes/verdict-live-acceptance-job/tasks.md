# Tasks

## 1. Job
- [x] 1.1 `verdict acceptance run`: six checks from the installed Verdict, result file with digests, keep N.
- [x] 1.2 Independent 2-turn tool check; precision/recall with account-level removal.
- [x] 1.3 Green only with an eligible FREE route (test + mutation); choose FAIL until M2.
- [x] 1.4 `verdict acceptance status`; `harness prime status --json`.

## 2. Install, doctor, alert
- [x] 2.1 `verdict-acceptance` unit in the `user_services` registry (hourly, dry-run notify, exit 3 = success).
- [x] 2.2 Doctor "Live acceptance" section; red or > 2 h is an issue.
- [x] 2.3 One alert per regression streak and one on recovery; state-file dedupe.

## 3. Live
- [x] 3.1 Install on the operator host with `verdict setup services`; see a recorded run.
- [x] 3.2 Two red runs on the host: doctor red, notification state recorded, no email (never green).
- [ ] 3.3 Switch the unit to `--notify send` in the change that makes the job green (after M2).
