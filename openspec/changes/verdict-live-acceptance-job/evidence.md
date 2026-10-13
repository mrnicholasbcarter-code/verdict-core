# Evidence: verdict-live-acceptance-job

Host: operator host `verdict`, live OmniRoute gateway http://127.0.0.1:20128,
real `~/.verdict`. Branch `story/acceptance-job-20261013` (on
`story/services-alerting-20261013` b59cebcc). Not yet on main.

## Install (2026-10-13T14:12:17Z, branch head 984ae11a + e44c728d)

`verdict setup services --json` (no exported gateway key) exit 0; key source
`key_file`; it wrote `verdict-acceptance.{service,timer}` and re-rendered the
prober and Prime sync units. Its own `setup --check` part reported every unit `ok`:

```
[Unit]
Description=Verdict live acceptance checks
After=network-online.target
[Service]
Type=oneshot
# No secret here: the command resolves the gateway key itself
# (credentials.env, ~/.config/verdict/omniroute-api-key, Prime auth.json).
WorkingDirectory=~/dev/verdict-acceptance-job
ExecStart="~/dev/verdict-core/.venv/bin/python" -m verdict acceptance run --notify dry-run
SuccessExitStatus=3
TimeoutStartSec=50min
[Unit]
Description=Start Verdict live acceptance checks
[Timer]
OnBootSec=10min
OnCalendar=hourly
Persistent=true
Unit=verdict-acceptance.service
[Install]
WantedBy=timers.target
```

```
$ systemctl --user is-active verdict-acceptance.timer verdict-prove-at-rest.timer verdict-prime-sync.timer
active
active
active
```

## Scheduled run, no manual action (timer, 15:00:10Z, source 79d0f399)

```
Result=success
ExecMainStartTimestamp=Tue 2026-10-13 15:00:10 UTC
ExecMainExitTimestamp=Tue 2026-10-13 15:01:48 UTC
ExecMainStatus=3
2026-10-13T15:00:10+00:00 verdict systemd[817]: Starting verdict-acceptance.service - Verdict live acceptance checks...
2026-10-13T15:01:48+00:00 verdict python[3729238]:   - result  RED
2026-10-13T15:01:48+00:00 verdict systemd[817]: Finished verdict-acceptance.service - Verdict live acceptance checks.
2026-10-13T15:01:48+00:00 verdict systemd[817]: verdict-acceptance.service: Consumed 39.464s CPU time, 216.8M memory peak, 0B memory swap peak.
20261013T135854Z.json
20261013T141225Z.json
20261013T141743Z.json
20261013T142647Z.json
20261013T150010Z.json
serve-receipts.db
state.json
{
  "alerted": false,
  "last_result": "~/.verdict/acceptance/20261013T150010Z.json",
  "last_status": "red",
  "red_streak": 5,
  "seen_green": false
}
```

Result `~/.verdict/acceptance/20261013T150010Z.json`:

| check | passed | exit | reason |
|---|---|---|---|
| doctor | False | 1 | 5 doctor issue(s): authoritative documentation preflight did not pass |
| eligibility | False | 0 | 0 FREE tool-capable routes eligible; precision unmeasured < 95%; recall 0% < 90% |
| choose | False | 2 | not implemented: exit 2 __main__.py choose: error: the following arguments are required: --task-class |
| route | False | 1 | no route (exit 1): fail_closed — empty free∩active ∩ fresh-passport ∩ confirmed intersection |
| serve | False | 503 | HTTP 503: {"error":{"message":"fail_closed — empty free∩active ∩ fresh-passport ∩ confirmed intersection","type":"invalid_request_error"},"decision":{ |
| harness-prime | False | 0 | Prime is not pointed at verdict serve |

Eligibility summary: `{"available": 0, "by_reached_stage": {"AVAILABLE": 0, "DISCOVERED": 1090, "ENTITLED": 7382, "HEALTHY": 0, "NONE": 0, "SELECTED": 0, "TASK_ELIGIBLE": 0}, "discovered": 8472, "eligible": 0, "entitled": 7382, "healthy": 0, "selected": null}`.
Independent 2-turn check: 12 unlisted live FREE routes sampled
(account-blocked 8, fail
3, pass
1); recall estimate
0.0, estimated missed 47 of
568. Exit status 3 = red result; unit `Result=success`
because of `SuccessExitStatus=3`.

## Doctor (14:29Z, after run 4)

`verdict doctor --json` exit 1, `status: issues_found`, `acceptance` key:

```
{
 "age_seconds": 75,
 "eligible_free": 0,
 "failed": [
  "doctor",
  "eligibility",
  "choose",
  "route",
  "serve",
  "harness-prime"
 ],
 "path": "~/.verdict/acceptance/20261013T141743Z.json",
 "state": "red"
}
```

Sections: `[{"detail": "service inactive/dead, last result success", "label": "verdict-acceptance.timer", "state": "ok"}, {"detail": "red: ~/.verdict/acceptance/20261013T141743Z.json, 1 min old", "label": "Live acceptance", "state": "failed"}]`

## Notification

On the host the job has never been green (`state.json`: `seen_green: false`,
`red_streak: 5`, `alerted: false`), so no email was prepared. That is correct:
the red state after install is known (M2 not built).

Drill in a scratch `VERDICT_HOME=/tmp/acc/drill-home`, with `state.json` seeded
by hand as "was green, red once" and a real run (14:22:33Z): notification
`{"event": "regression", "mode": "dry-run", "red_streak": 2}` and one file
`email-20261013T142233Z-regression.json` (mode 0600) to nick@nickcarter.dev,
subject "Verdict acceptance RED: doctor, eligibility, choose, route, serve,
harness-prime". `composio execute GMAIL_SEND_EMAIL --dry-run -d @<file>`
returned `"successful": true, "dryRun": true`. Nothing was sent.

## Found by the live runs and fixed on the branch
- serve exited 3: authenticated `verdict serve` needs `VERDICT_RECEIPTS_DB`;
  the job gives it its own `acceptance/serve-receipts.db` (eadccb57).
- With 0 listed, the independent check was skipped; it now always runs (eadccb57).
- Under systemd, route reported `http 401` and doctor "No gateway URL": the
  checks did not get the job's gateway; fixed (fd9757be).


## Final head 1683e96a (manual start of the unit, 15:33:42Z)

```
Result=success
ExecMainStartTimestamp=Tue 2026-10-13 15:33:42 UTC
ExecMainExitTimestamp=Tue 2026-10-13 15:35:43 UTC
ExecMainStatus=3
20261013T135854Z.json
20261013T141225Z.json
20261013T141743Z.json
20261013T142647Z.json
20261013T150010Z.json
20261013T153343Z.json
serve-receipts.db
state.json
{
  "alerted": false,
  "last_result": "~/.verdict/acceptance/20261013T153343Z.json",
  "last_status": "red",
  "red_streak": 6,
  "seen_green": false
}
```

`verdict doctor --json` exit 1, `status: issues_found`, `acceptance`:
`{"age_seconds": 34, "eligible_free": 0, "failed": ["doctor", "eligibility", "choose", "route", "serve", "harness-prime"], "path": "~/.verdict/acceptance/20261013T153343Z.json", "state": "red"}`; unit rows: verdict-prove-at-rest.timer ok,
verdict-prime-sync.timer ok, verdict-acceptance.timer ok; "Live acceptance" failed.
`verdict acceptance status --json` exit 1 (same newest result).

Reachability: `scripts/check_reachability.py` reports 0 NEW findings.
