# Evidence: verdict-user-services-alerting

Host: operator host `verdict` (user nick), 2026-10-13. Branch
`story/services-alerting-20261013`. Commands ran from the worktree checkout with
`/home/nick/dev/verdict-core/.venv/bin/python -m verdict` (`PYTHONPATH` = worktree).
This is branch evidence, not merged-main evidence.

## Before (unit hand-copied on Sep 27, no key in its environment)

```
2026-10-13T10:15:47Z sh[3497336]: BLOCKED: cannot read live inventory from http://127.0.0.1:20128/v1/models: HTTP Error 401: Unauthorized
2026-10-13T10:15:47Z verdict-prime-sync.service: Main process exited, code=exited, status=2/INVALIDARGUMENT
```

`verdict-prove-at-rest` was not installed (`~/.config/systemd/user` had only the
two prime-sync files).

## Install: `verdict setup services` (d0f7baa9)

The first install at 40af6218 failed on `restart`:
`WorkingDirectory= path is not absolute: "/home/nick/dev/verdict-services-alerting"`
(quoted path). Fixed in d0f7baa9; `systemd-analyze verify` now runs in the tests.

```
  + gateway key for services  OK   key_file
  - verdict-prove-at-rest.service  WRITTEN
  - verdict-prime-sync.service  WRITTEN
  - ran  systemctl --user daemon-reload
  - ran  systemctl --user enable --now verdict-prove-at-rest.timer
  - ran  systemctl --user restart verdict-prove-at-rest.service
  - ran  systemctl --user enable --now verdict-prime-sync.timer
  - ran  systemctl --user start verdict-prime-sync.service
  + verdict-prove-at-rest.timer  OK   service active/running, last result success
  + verdict-prime-sync.timer  OK   service inactive/dead, last result success, last run Tue 2026-10-13 10:20:21 UTC
ISSUES (0)  System is healthy! All checks passed.
```

Second run on the same head: `changed: false`, commands = the two `enable --now`.

## Prime sync after the fix (timer-driven, no env key)

```
2026-10-13T10:20:21Z Finished verdict-prime-sync.service
2026-10-13T10:35:23Z Finished verdict-prime-sync.service
2026-10-13T10:50:52Z Finished verdict-prime-sync.service
  live models: 8578  added: 2  removed: 0
  backup: /home/nick/.prime/agent/models.json.verdict-sync-20261013T105052Z.bak
```

## Drill: stop the prober timer

```
$ systemctl --user stop verdict-prove-at-rest.timer
$ verdict doctor --json         -> exit 1
  issue: verdict-prove-at-rest.timer is inactive. Run: systemctl --user enable --now verdict-prove-at-rest.timer
$ verdict setup --check         -> exit 1
  - verdict-prove-at-rest.timer  INACTIVE
  ISSUES (1)
$ systemctl --user start verdict-prove-at-rest.timer
```

## Secret check
The real gateway key values (key file and Prime auth.json) were searched, by
value, in the four installed unit files and every captured output above: not found.
