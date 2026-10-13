# Design: production outcome and lesson writers

`run_golden_path` injects the default `SessionLedger` into `DagRuntime`. The runtime writes once at the terminal settlement of each dispatched worker attempt and once per reviewer attempt reported by the reviewer adapter. Fault-injected attempts are excluded because simulated evidence cannot affect live ranking. Supervisor and improve already execute this production path, so they inherit the writer without a duplicate source of truth.

`run_cycle` writes a lesson only after `verify_run_receipt` accepts the receipt. `MemoryGate` owns policy, redaction, authority verification, and persistence. The value is the fixed layer lesson plus measured evidence; provenance carries cycle id, run id, receipt path, and receipt SHA-256. The existing BOD-306 read-only pack compiler admits `lessons` beside the other gated fact namespaces.

Storage failure is fail-open for this advisory evidence and is exposed by `memory_written` in the learned cycle record. No session-log ingestion is added; that remains with the separate memory-loop story.

All default Verdict state paths resolve through `verdict.paths.verdict_home` (BOD-330), including the outcome ledger and MemoryGate database. A syntax guard rejects any new `Path.home() / ".verdict"` default outside `paths.py`.
