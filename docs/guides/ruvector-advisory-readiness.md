# RuVector advisory readiness

`verdict.ruvector_adapter.RuVectorAdapter` is the optional boundary for a
RuVector/RVF executable. It negotiates the executable version and declared
commands through bounded argv-only probes. Shell syntax, unbounded output, and
raw process diagnostics do not cross the boundary.

Readiness is `ready` only when both probes succeed, version output is
recognized, and all requested commands are advertised. Missing, timed-out,
truncated, or unsupported capability evidence produces `degraded` or
`unavailable`; advisory retrieval remains disabled in those states.

This adapter does not access RuVector databases, mutate SONA weights, or
change deterministic eligibility. It reports a digestable readiness artifact
that a later storage/retrieval implementation can consume. SONA remains
observe-only until replay baselines, regression bounds, drift metrics, a kill
switch, and snapshot rollback are separately proven.
