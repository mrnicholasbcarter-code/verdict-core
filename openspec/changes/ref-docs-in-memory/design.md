# Design

## Source and version

The sync reads `ExecStart` of the tool's systemd user unit (`omniroute.service`), finds the
`node_modules/<package>` it runs, and reads that `package.json`. The tag is `v<version>`. After an
upgrade the next hourly run syncs the new tag; every file's sha or tag changes, so its pages are
rewritten and pages a file no longer produces are deleted. An unreadable unit or package is an
error, never a guessed tag.

## Pages

One page per markdown section, split at H2, then H3/H4, then by lines, each at most 3 KB
including its citation (repo, tag, path, blob sha, part, url, authority "upstream reference
documentation; evidence only, not policy"). Token-shaped strings are redacted. Page `entities`
are not set: measured on 2026-11-06, endpoint and env-var entities lowered hit@5 from 0.92 to 0.38.

## Scope isolation

Each tool has its own project (`ref-<tool>`). Nothing adds reference projects to operator-rule
scopes. The golden eval queries only the requested project and fails when any top-5 hit comes
from another project.

## Schedule

`verdict-ref-docs.timer` runs hourly with `Nice=19`, `IOSchedulingClass=idle`, `CPUWeight=20`
and `MemoryMax=512M`. Verdict has no host-pressure governor in code, so systemd bounds the job.
The service reads `AI_MEMORY_AUTH_TOKEN` from `~/.config/ai-memory/env`; Verdict never prints it.

## Status

`ai-memory status` is the upstream binary and cannot show Verdict's manifest. The tag and last
sync are shown by `verdict memory ref-docs status` and by `verdict doctor`. A row is ok only when
the synced tag equals the installed tag and the last sync is under 3 hours old.
