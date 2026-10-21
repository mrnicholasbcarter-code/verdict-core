## Local implementation and evidence
- [x] Write regression tests before implementation. Main: four failures (quota order, missing quota snapshot field, missing reserved slot, metered selected). Screen queue regression: missing request_rescreen fails.
- [x] Implement qualified subscription order, bounded last-attempt fallback, explicit fail-closed and persisted screen hints.
- [ ] Run every affected reference test file and record raw count.
- [ ] Run one full suite under 4G/1800s with four workers/loadfile.
- [ ] Run ruff once, strict mypy, reachability with zero NEW, OpenSpec strict validation and diff check.
- [ ] Parent merged-main installed-host acceptance (not claimed by PR).
