## 1. Cache and refresh
- [x] 1.1 Health cache records passive traffic and passive failures with `evidence_source`, newer-wins and negative-preserving rules.
- [x] 1.2 Passive identify folds health-matrix traffic and webhook events into the cache with `--refresh-health-cache`.
- [x] 1.3 Webhook receiver verifies signed events; registration reports OmniRoute refusals.

## 2. Units, doctor and docs
- [x] 2.1 The passive identify unit runs `--refresh-health-cache`; the interval stays three hours.
- [x] 2.2 Doctor requires a clean `health_refresh` in the passive report.
- [x] 2.3 Doctor names the passive source and its repair when the health cache has no usable route, and counts evidence by source.
- [x] 2.4 Doctor reports the OmniRoute webhook as ok or warning, never failed.
- [x] 2.5 `docs/CONFIGURATION.md` describes the passive health refresh.

## 3. Live acceptance
- [ ] (post-merge) 3.1 Reinstall with `verdict identify install-service`, run `systemctl --user start verdict-identify.service`, and confirm `verdict doctor` shows the Health cache row ok with passive traffic entries on the real host.
- [ ] (post-merge) 3.2 Confirm after the next scheduled passive run, with no manual action, that the Health cache row stays ok and the newest passive evidence age is under six hours.
