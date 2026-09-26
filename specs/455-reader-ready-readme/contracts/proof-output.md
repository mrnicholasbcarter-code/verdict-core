# Contract: Credential-Free Proof Output

## Command

```text
verdict quickstart --non-interactive --dry-run
```

## Canonical source

The existing deterministic flagship fixture and its human-readable renderer are authoritative. README and `quickstart.sh` must consume or validate that same path; they must not maintain independent routing or output logic.

## Required report semantics

The report must contain:

1. the credential-free quickstart identity;
2. selected route `demo/frontier-tools`;
3. exactly three excluded candidates;
4. each excluded candidate and its canonical named reason;
5. receipt `fixture:issue-35` with mode `deterministic_fixture`;
6. successful status after validation.

If the canonical renderer includes additional stable lines, the README example must include them unless implementation deliberately changes the renderer and its tests within FR-011's compatibility boundary.

## Exactness rule

A focused regression test must extract the fenced README proof-output block and compare it with the canonical rendered report. Normalization may remove only Markdown fence delimiters and one trailing newline. It must not omit, reorder, rewrite, or selectively compare report lines.

## Safety boundary

- No provider or gateway call.
- No credential read or persistence.
- No application-state write, including when run from an empty directory.
- Fixture receipt must not be described as production, live-provider, or independent repository-change proof.

## Failure behavior

Mismatch, malformed fixture data, missing exclusions, unresolved README links/fragments, network access, or state writes fail validation. Missing terminal/human evidence remains `not-run` or `blocked`; it is never inferred from automated success.
