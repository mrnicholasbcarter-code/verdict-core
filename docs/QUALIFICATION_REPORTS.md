# Qualification reports

`verdict.qualification_report` renders a deterministic, payload-free
explanation over one exact capability passport. It is intended for CLI and
operator surfaces that need to answer why a route is admitted or rejected.

Reports keep claimed and observed evidence separate, preserve evidence digests
and expiry, and evaluate requested capabilities through the passport's
observed-only, fail-closed resolver. Catalog claims and HTTP success therefore
cannot become admission merely by appearing in a report.

Endpoints are redacted by removing userinfo, query strings, and fragments.
The report contains no prompts, response bodies, credentials, or authorization
headers. `report_digest` is a canonical SHA-256 digest of the redacted report.

```python
from verdict.qualification_report import build_qualification_report

report = build_qualification_report(
    passport,
    required_capabilities=("chat.completions", "tools"),
)
print(report.to_dict())
```

An inventory report with no requirements is explicitly marked with the
`no hard requirements supplied` limitation. Callers must provide hard
requirements before using `passed` as an admission result.
