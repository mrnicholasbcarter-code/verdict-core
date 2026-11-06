# Upstream reference docs in ai-memory

Verdict keeps the docs of the tools it depends on as cited, read-only pages in ai-memory, at the
version that actually runs. Today this covers OmniRoute (project `ref-omniroute`).

## What runs

- `verdict memory ref-docs sync` reads the tag of the installed release from the tool's systemd
  user unit (`omniroute.service` runs `node_modules/omniroute`, whose `package.json` gives the
  version), lists the GitHub git tree at that tag with `gh api`, and writes one page per markdown
  section (at most 3 KB) into the tool's project. `docs/openapi.yaml` becomes one page group per
  API tag. Every page ends with a citation: repo, tag, path, blob sha, part and url.
- A manifest at `$VERDICT_HOME/ref-docs/<workspace>--<project>.json` stores each file's blob sha
  and pages. A file is rewritten only when its sha or the render format version
  changes; pages of removed files are deleted.
  A run with no upstream change writes 0 pages.
- `verdict-ref-docs.timer` runs the sync hourly at low priority. `verdict setup` installs it as
  host prerequisite `host.ref-docs-timer`; `verdict memory ref-docs install-service` does the
  same by hand.
- `verdict memory ref-docs status` and `verdict doctor` show each project's synced tag, installed
  tag, last sync and page count. A row fails when the docs were never synced, are behind the
  installed tag, are older than 3 hours, or the installed version cannot be read.

## How to query

Request the reference project only when the task touches that tool:

```python
await mcp.call_tool("ai-memory", "memory_query",
                    {"workspace": "default", "project": "ref-omniroute",
                     "query": "auto-combo candidates endpoint", "limit": 5})
```

Reference pages are evidence about the upstream tool, not policy. They are never part of
operator-rule scopes.
