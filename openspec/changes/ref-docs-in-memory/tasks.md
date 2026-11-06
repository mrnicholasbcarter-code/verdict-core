# Tasks

## 1. Sync
- [x] 1.1 Section splitting, oversize split, fences, redaction, OpenAPI per tag, blob-sha skip and removed-file deletion: `tests/test_ref_docs.py`
- [x] 1.2 `verdict/ref_docs.py` with installed-tag resolution from the tool's systemd unit and `status()`
- [x] 1.3 `verdict memory ref-docs sync|status`
- [x] 1.4 Integration: fake `gh` tree fixture into a temp ai-memory server: `tests/test_ref_docs_integration.py`

## 2. Schedule and checks
- [x] 2.1 `verdict/ref_docs_service.py` hourly timer with resource controls, `verdict memory ref-docs install-service`
- [x] 2.2 `host.ref-docs-timer` in `verdict setup`; doctor rows for the timer and each reference project

## 3. Recall
- [x] 3.1 `scripts/memory_recall_eval.py` with tests; the golden set stays outside the repository
- [x] 3.2 Live, 2026-11-06, rebuilt 40-question set (kept outside the repo): scoped hit@5 is 0.80 with the operator pages alone and 0.875 with the reference pages added (fresh stores), 0.825 on the live store, with 0 foreign hits in every scoped run. The reference pages do not lower recall. The absolute 0.90 bar from the ticket is not met by the operator corpus itself (5 vocabulary or tokenizer misses that also miss without reference pages); this is an open operator decision outside this change

## 4. Docs
- [x] 4.1 `docs/guides/reference-docs.md`

- [ ] (post-merge) Install the timer from merged main with `verdict setup --apply --allow host.ref-docs-timer`; `verdict doctor` shows the timer and `default/ref-omniroute` rows ok
- [ ] (post-merge) After one hourly run, `verdict memory ref-docs status` shows a last sync newer than the install and 0 pages written in the journal
