# Identify maintenance

`verdict identify install-service --dry-run` previews the replacement user units. `verdict identify install-service` installs them; `verdict identify uninstall-service` removes them. `verdict setup` offers the same installer after explicit confirmation. Installation never enables the retired prove-at-rest or census units; keep those disabled.

Approved defaults are defined together in `verdict/identify_service.py`: passive every **3 hours**; a separate **daily** screen, using `--capacity free,subscription --max-per-provider 1 --max-routes 8`. A screen uses the existing consented missing-proof flow and skips fresh proof. Enabling unchanged units does not manually trigger a screen.

The passive service runs `verdict identify --passive --json --save-report`. The optional save flag atomically writes `$VERDICT_HOME/identify-latest.json` (default `~/.verdict/identify-latest.json`) with mode 0600. Without that flag passive identify remains read-only and makes zero model calls. JSON is also retained in the service journal.

Check `verdict doctor`, `systemctl --user status verdict-identify.timer verdict-identify-screen.timer`, and `journalctl --user -u verdict-identify.service -u verdict-identify-screen.service`. Doctor checks both timers and the passive report: missing, future, six-hour-old, or source-error reports are red. Installed-runtime rows show the package/build and state scope. Timer rows show last attempt, completion, result and next run; unknown values remain explicit. Health-cache evidence still must contain a fresh or usable stale route. This does not relax the eligible-free-route requirement in hourly acceptance.

These are installation and diagnostic interfaces, not a claim of real-host acceptance. The parent must install merged main and verify the units and report after freshness before declaring the story Done.
