---
name: verdict-hydrate
description: Use as the controller right before handing a verdict-core story to an implementation agent. Builds one bounded context pack from the GitHub issue, the approved OpenSpec change, the research findings and Verdict's own worker-context builder, so the implementer starts from facts instead of re-exploring the repository.
---

# Hydrate a story into a context pack

The controller prepares; the implementer consumes. One pack per story, written once, reused while its stamp is valid. The pack quotes the authorities (GitHub issue, OpenSpec change, source); it never replaces them.

## When
- After research (if the story needed it) and after the OpenSpec change is approved (if the story changes behaviour).
- Before every build or fix attempt: check the stamp first and rebuild only when it is stale.

## Steps
1. Write `/tmp/claude-takeover/hydrate/issue-<N>.json`:
   ```json
   {"unit": "issue-<N>", "issue": <N>, "story": "#<N>",
    "objective": "<one or two sentences from the issue's Goal>",
    "openspec_change": "<change name, or empty for a no-behaviour-change fix>",
    "research_file": "<path, or empty>",
    "owned_files": ["<files the implementer may write>"],
    "context_files": ["<files it must read but not write>"],
    "keywords": ["<3-6 terms for ADR and rule lookups>"],
    "acceptance": ["<each acceptance check from the issue>"],
    "verification_command": ["uv", "run", "--frozen", "python", "-m", "pytest", "-q", "<focused test files>"]}
   ```
   Get `owned_files` and `context_files` from the code graph (step 3 says how), not from guessing. When an OpenSpec change exists, its tasks decide the owned files.
2. Build the pack (read-only on the repo):
   `cd <main checkout> && .venv/bin/python -I .claude/skills/verdict-hydrate/scripts/build_pack.py /tmp/claude-takeover/hydrate/issue-<N>.json --repo <worktree or repo> --out /tmp/claude-takeover/hydrate/issue-<N>-pack.md`
3. Fill the one section the script leaves as `_TODO_`, "Symbols, callers, tests", with the two code-intelligence tools. This is the step that saves the implementer from re-discovering the code, so do it properly; keep the section under 40 lines, each entry as `path:symbol`.
   First make sure both tools see current code: the main checkout must be at origin/main (`git -C <main checkout> status --porcelain --untracked-files=no` empty, then `git merge --ff-only origin/main`). If it is not clean, stop and report; do not hydrate from stale code.
   **codebase-memory (the code graph; fast, use it first).** Project name `home-nick-dev-verdict-core`.
   - `search_graph` (name_pattern or query, optionally file_pattern) to find the functions, classes and commands the story touches. Never guess a path.
   - `trace_path` with `direction: "inbound"`, `include_tests: true`, `depth: 2` on each function that will change: this is the caller list and the tests that cover it (measured 2026-11-24: 35 callers of `_probe_order` in one call, under a second).
   - `trace_path` with `direction: "outbound"` when the story changes what a function relies on.
   - `get_code_snippet` for the two or three bodies the implementer must read; `get_architecture` only for a story that spans subsystems.
   - `check_index_coverage` for every file you cite; if a file is not covered, read it directly and say so in the pack.
   **Serena (the language server; exact, slower to start).** Use it where the graph is not enough: precise references to a symbol, the members of a class, a file overview without reading the file.
   - It needs its manual and an active project once per session: call `initial_instructions`, then `activate_project` with the absolute path of the main checkout. The first symbol query after activation can take minutes while the language server indexes; later ones are fast.
   - Through `serena_repl`: `s.lsp.get_symbols_overview(relative_path=...)`, `s.lsp.find_symbol(name_path_pattern=..., include_body=False, depth=1)`, `s.lsp.find_referencing_symbols(name_path=..., relative_path=...)`. Use `s.info("lsp")` for exact signatures.
   - Read-only here. One Serena server serves one project directory: it reads and edits the main checkout, not an agent's worktree. Never use `s.edit.*`, `s.lsp.rename_symbol` or `s.shell` from this skill.
   Confirm anything surprising against the source before it goes in the pack.
4. Check the size: aim for under 20 KB. If the native context was truncated on something the story needs, add that file to `context_files` and rebuild; don't paste whole files.
5. Hand the implementer the pack path. Its instruction is: read the pack once, build the OpenSpec tasks in order (or the issue's acceptance checks when there is no change), and report anything in the pack that the source contradicts.

## Reuse
`build_pack.py <unit.json> --repo <repo> --check <pack.md>` prints `valid` and exits 0 when the story's own files on origin/main (owned files, context files and the OpenSpec change directory), the issue's text (title, body and state; comments and labels do not count) and the OpenSpec `tasks.md` hash are unchanged. A merge to main that touches none of those files leaves the pack valid, so list every file the pack's facts depend on in `owned_files` or `context_files`; otherwise it prints what changed and exits 3. Rebuild only then. A fix round after review reuses the same pack unless the stamp is stale.

## Limits
- The pack is evidence, not authority: it never authorises a route, a scope change or a skipped gate.
- No secrets and no contents of `~/.verdict` go into a pack.
- No second planning system: no roadmap, state or phase files. GitHub Issues and OpenSpec hold the plan.
