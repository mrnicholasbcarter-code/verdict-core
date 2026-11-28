export const meta = {
  name: 'story-pipeline',
  description: 'Take verdict-core GitHub issues to review-ready PRs: optional research, OpenSpec hand-off, context pack, build, independent cross-family review, bounded fix loop, CI check. Never merges.',
  whenToUse: 'Run one or more verdict-core issues through the standard pipeline. args: {stories: [{issue: 1300, slug: "metadata-carry-forward", research: false, stage: "build", size: "S", openspec_change: "", pack_path: "/tmp/claude-takeover/hydrate/issue-1300-pack.md", notes: "optional scope notes"}], maxParallel: 3}. For stage "build" the controller builds the context pack itself first (verdict-hydrate skill) and passes pack_path; the workflow only checks its stamp. stage "spec" stops after opening the OpenSpec design PR for operator approval; stage "build" (default) builds from the approved change or, for a no-behaviour-change fix, from the issue.',
  phases: [
    { title: 'Research', detail: 'read-only findings file per story that asks for it' },
    { title: 'Spec', detail: 'stage "spec" only: draft the OpenSpec change as a design PR, independent review with at most two fix rounds, then stop for approval' },
    { title: 'Hydrate', detail: 'check the stamp of the context pack the controller built; never author it' },
    { title: 'Build', detail: 'test-first in its own worktree from the pack, local review, push, PR' },
    { title: 'Review', detail: 'independent cross-family review of the exact PR head' },
    { title: 'Spec check', detail: 'trace every spec scenario and ticked task to a test and to evidence' },
    { title: 'Fix', detail: 'at most two rounds of review fixes' },
    { title: 'CI', detail: 'wait for checks on the exact head; one repair round if red; then verify review and checks on one commit' },
    { title: 'Retro', detail: 'append the run to the ledger; propose lessons from recurring defects' },
  ],
}

// ---------------------------------------------------------------- inputs
const input = args || {}
const stories = Array.isArray(input.stories) ? input.stories : []
if (!stories.length) {
  return { error: 'pass args.stories: [{issue, slug, research?, notes?}]' }
}
// The machine has 8 cores shared with production services; three builders is the limit.
const MAX_PARALLEL = Math.max(1, Math.min(3, Number(input.maxParallel) || 3))
const MAX_FIX_ROUNDS = 2
// Machine-local: the operator's main checkout, which this workflow runs from and builds worktrees under.
const REPO = '/home/nick/dev/verdict-core'
// The review and readiness scripts are versioned in the repository (.claude/tools/), so they are the ones in the main checkout.
const REVIEW = `${REPO}/.claude/tools/review_pr.sh`
// A review always runs through the key wrapper: a shell started before a key rotation holds the old key and the review
// then fails with HTTP 401 (2026-11-28). The wrapper supplies the current gateway key without printing it.
const KEYWRAP = '~/.config/verdict/with-new-gateway-key.sh'
// Deterministic readiness check: review record and check runs on the PR's current head commit.
const VERIFY = `${REPO}/.claude/tools/verify_ready.sh`
// Machine-local pipeline state (review records, research findings, context packs); not versioned.
const HYDRATE_DIR = '/tmp/claude-takeover/hydrate'
// The workflow's own review is stored apart from the record the author made before pushing.
const REVIEW_SUFFIX = '.independent'
// Builders and researchers run on Codex; review and readiness must use the same author family.
const AUTHOR_FAMILY = 'codex'
// Single-quote a value for the shell, so notes with $, backticks or quotes are passed literally.
const shq = (v) => `'${String(v).replace(/'/g, `'\\''`)}'`
// A workflow agent that ends its turn to wait for background work is made to report at once and cannot be
// resumed (2026-11-24: the #1315 builder lost a finished build this way while its review ran in the background).
const FOREGROUND =
  ` Run the review script, the push and every other long command in the FOREGROUND with a timeout of up to 10 minutes, ` +
  `and re-run a command that times out; never start one in the background and never end your turn to wait: in this ` +
  `workflow an agent that stops is forced to report immediately and is not resumed. Run the lint gates and validators once ` +
  `per commit, when the change is complete, not after every edit, and then re-run only what failed. If the review fails with zero tokens ` +
  `or timeouts (the reviewer route is rate-limited), retry it a few minutes later, up to an hour, before reporting blocked. ` +
  `Reviews are paid for from a limited subscription window (2026-11-24: one build ran eleven pre-push review rounds, ` +
  `10 million tokens, and exhausted it twice). Run AT MOST TWO review rounds before the first push. If findings remain ` +
  `after the second, do not review again: push, list every open finding in the PR body under "Open review findings", ` +
  `and return pr_open. The workflow's own bounded review loop takes it from there.`
const worktreeOf = (s) => `${REPO}/.worktrees/${s.slug}`
const branchOf = (s) => s.branch || `story/${s.issue}-${s.slug}`
const tag = (s) => `#${s.issue}`
// Small stories (size "S") use the builder with a restricted tool set; neither Codex builder has advisor.
const builderOf = (s) => (String(s.size || '').toUpperCase() === 'S' ? 'verdict-builder-small' : 'verdict-builder')

// ---------------------------------------------------------------- schemas
const RESEARCH = {
  type: 'object',
  properties: {
    findings_path: { type: 'string' },
    recommendation: { type: 'string' },
    operator_decisions: { type: 'array', items: { type: 'string' } },
    split_proposed: { type: 'boolean' },
  },
  required: ['findings_path', 'recommendation', 'operator_decisions', 'split_proposed'],
}
const BUILD = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['pr_open', 'blocked'] },
    pr_number: { type: 'integer' },
    head_sha: { type: 'string' },
    red_green_evidence: { type: 'string' },
    not_done: { type: 'string' },
    blocked_reason: { type: 'string' },
  },
  required: ['status', 'not_done'],
}
const REVIEW_RESULT = {
  type: 'object',
  properties: {
    complete: { type: 'boolean' },
    exit_code: { type: 'integer' },
    tool_failures: { type: 'integer' },
    head_sha: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: { path: { type: 'string' }, line: { type: 'string' }, text: { type: 'string' } },
        required: ['path', 'text'],
      },
    },
    raw_first_line: { type: 'string' },
  },
  required: ['complete', 'exit_code', 'findings', 'raw_first_line'],
}
const PACK = {
  type: 'object',
  properties: {
    exit_code: { type: 'integer' },
    output: { type: 'string' },
    bytes: { type: 'integer' },
  },
  required: ['exit_code', 'output'],
}
const READY = {
  type: 'object',
  properties: {
    exit_code: { type: 'integer' },
    line: { type: 'string' },
    head_sha: { type: 'string' },
  },
  required: ['exit_code', 'line'],
}
const SPEC_CHECK = {
  type: 'object',
  properties: {
    checked: { type: 'boolean' },
    head_sha: { type: 'string' },
    contract: { type: 'string' },
    validators_pass: { type: 'boolean' },
    scenarios_total: { type: 'integer' },
    scenarios_covered: { type: 'integer' },
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          kind: {
            type: 'string',
            enum: [
              'scenario_without_test',
              'test_cannot_fail',
              'task_ticked_without_evidence',
              'open_task_not_post_merge',
              'post_merge_marks_build_work',
              'code_outside_spec',
              'spec_contradicts_code',
              'docs_describe_what_does_not_run',
              'validator_failure',
            ],
          },
          path: { type: 'string' },
          text: { type: 'string' },
        },
        required: ['kind', 'path', 'text'],
      },
    },
  },
  required: ['checked', 'contract', 'validators_pass', 'gaps'],
}
const CI_RESULT = {
  type: 'object',
  properties: {
    state: { type: 'string', enum: ['green', 'red', 'pending'] },
    head_sha: { type: 'string' },
    failed_checks: { type: 'array', items: { type: 'string' } },
    failure_summary: { type: 'string' },
  },
  required: ['state', 'failed_checks'],
}

// ---------------------------------------------------------------- helpers
// The workflow's own cross-family review: its own brief, its own record (reviews/<sha>.independent.json).
// It never reuses the record the author made before pushing. Pass or fail is the script's exit code.
const runReview = (s, pr, context) =>
  agent(
    `Run exactly this one command from ${REPO} and report its output. Do nothing else: no edits, no other commands except reading the output.\n\n` +
      `REVIEW_AUTHOR_FAMILY=${AUTHOR_FAMILY} REVIEW_RECORD_SUFFIX=${REVIEW_SUFFIX} ${KEYWRAP} ${REVIEW} ${pr} ${shq(context)}; echo "EXIT=$?"\n\n` +
      `It can take up to 40 minutes; run it in the foreground with a long timeout. Do not pipe it through anything. Its summary line looks like ` +
      `"PR #N head <full sha> review=<status> model=<m> files=<done>/<selected> findings=<n> tool_calls=<n> tool_failures=<n>" ` +
      `(it may be preceded by "(stored result for unchanged head)"), followed by one line per finding ("  - <path> <line> <text>"). ` +
      `The last line is "EXIT=<code>": put that number in exit_code (use 99 if the line is missing or the command timed out). ` +
      `Put the summary line in raw_first_line, the full sha in head_sha and tool_failures from the line. ` +
      `Set complete=true only if EXIT=0 and review=complete and done equals selected and selected > 0 and tool_failures=0. ` +
      `Otherwise set complete=false. Copy each finding's text in full; do not summarise, judge or drop any.`,
    { label: `review ${tag(s)}`, phase: 'Review', schema: REVIEW_RESULT, effort: 'low' },
  )

// Spec verification: does the head do what the contract says, and prove it? Read-only.
const runSpecCheck = (s, pr) =>
  agent(
    `Verify verdict-core PR #${pr} (issue #${s.issue}) against its contract. Read-only: no edits, no commits, no pushes, no test-suite runs.\n` +
      `Work in a throwaway detached worktree of the PR head: \`cd ${REPO} && git fetch -q origin && SHA=$(gh pr view ${pr} --json headRefOid --jq .headRefOid) && ` +
      `git worktree add --detach /tmp/spec-check-${s.issue} $SHA\`; remove it with \`git worktree remove --force /tmp/spec-check-${s.issue}\` when done.\n` +
      `Contract: ` +
      (s.openspec_change
        ? `the OpenSpec change openspec/changes/${s.openspec_change}/ (spec delta scenarios, tasks.md, design.md, proposal.md).`
        : `no OpenSpec change is named. If the diff adds one under openspec/changes/, use it; otherwise the contract is the issue's acceptance checks (\`gh issue view ${s.issue}\`).`) +
      (s.notes
        ? `\nScope notes from the controller: ${s.notes}\n` +
          `The notes may limit which acceptance checks this pull request claims to deliver. Do not report a gap for a check the notes put out of scope; ` +
          `instead confirm that the PR body says it is not delivered and that the PR does not close the issue (a closing keyword for #${s.issue}, or a body ` +
          `that claims an out-of-scope check, is docs_describe_what_does_not_run). Everything the notes leave in scope is checked in full.`
        : '') +
      `\nDo all of this and report every gap; do not fix anything:\n` +
      `1. Validators: in that worktree run \`uv run --frozen python scripts/check_openspec_vendor.py --base origin/main\` and ` +
      `\`uv run --frozen python scripts/check_openspec_tasks.py\` (read their --help first if the flags differ). Any failure is a validator_failure gap.\n` +
      `2. Scenarios: list every "#### Scenario" in the change's spec delta (or every acceptance check). For each, name the test(s) in the diff that assert it, ` +
      `open each test and confirm it would fail if the behaviour were absent (an assertion that is always true, a guard that skips the assertions, ` +
      `or a test that never reaches the code path is test_cannot_fail). A scenario with no such test is scenario_without_test.\n` +
      `3. Tasks: every "- [x]" task needs evidence in the diff or the PR body (code, test, or recorded output). A live-check task ticked without recorded output is ` +
      `task_ticked_without_evidence. Every "- [ ]" task must be "(post-merge)"; and a "(post-merge)" task that is really build work (creating a module, ` +
      `wiring a command or service, adding a doctor row) is post_merge_marks_build_work.\n` +
      `4. Scope: every changed product file (\`git diff --name-only origin/main...$SHA\`) must belong to a task or a scenario; otherwise code_outside_spec.\n` +
      `5. Consistency: proposal.md, design.md, tasks.md, the spec delta and the code must agree. Text that promises a flag, endpoint or behaviour the code ` +
      `does not have is docs_describe_what_does_not_run; a scenario the code contradicts is spec_contradicts_code.\n` +
      `Set checked=true only if you completed all five steps. Give each gap the file path and one precise sentence a developer can act on.`,
    { label: `spec-check ${tag(s)}`, phase: 'Spec check', schema: SPEC_CHECK, effort: 'medium' },
  )

const runCi = (s, pr) =>
  agent(
    `Wait for GitHub checks on verdict-core PR #${pr} and report the result. Do nothing else: no edits, no pushes, no reruns.\n\n` +
      `Loop at most 40 times: run \`cd ${REPO} && gh pr checks ${pr}\`; if no check is pending, queued or in progress, stop; ` +
      `otherwise wait 45 seconds (use a background wait, not a foreground sleep, if foreground sleep is blocked) and repeat.\n` +
      `Then report: state=green if every check passed or was skipped; state=red if any failed (list their names in failed_checks, ` +
      `and for each failed test job put the failing test names from \`gh run view <run-id> --job <job-id> --log-failed\` in failure_summary); ` +
      `state=pending if checks were still running after the last loop. head_sha from \`gh pr view ${pr} --json headRefOid --jq .headRefOid\`.`,
    { label: `ci ${tag(s)}`, phase: 'CI', schema: CI_RESULT, effort: 'low' },
  )

const reviewContext = (s) =>
  `verdict-core issue #${s.issue}. Read the issue's acceptance check as the bar. ${s.notes || ''} ` +
  `Check that the change meets the acceptance check, that every new test fails without the fix, that nothing weakens an existing test, ` +
  `gate or safety invariant, that no test writes outside a temp home, and that OpenSpec tasks marked done are really done.`

// ---------------------------------------------------------------- stages
const research = async (s) => {
  if (!s.research) return { story: s }
  const r = await agent(
    `Research verdict-core issue #${s.issue} before its spec is written. ${s.notes || ''}\n` +
      `Write the findings file to /tmp/claude-takeover/research/issue-${s.issue}.md as your instructions describe.`,
    { agentType: 'verdict-researcher', label: `research ${tag(s)}`, phase: 'Research', schema: RESEARCH },
  )
  if (!r) return { story: s, stopped: 'research agent returned nothing' }
  if (r.operator_decisions.length || r.split_proposed) {
    log(`${tag(s)} stops after research: ${r.operator_decisions.length} operator decision(s), split proposed: ${r.split_proposed}`)
    return { story: s, research: r, stopped: 'needs operator: decisions or a proposed split, see findings file' }
  }
  return { story: s, research: r }
}

// Stage "spec": the OpenSpec change is the hand-off contract. The operator approves it before any build.
const spec = async (prev) => {
  if (prev.stopped || prev.story.stage !== 'spec') return prev
  const s = prev.story
  const b = await agent(
    `Draft the OpenSpec change for verdict-core issue #${s.issue} as a design-only pull request. Do not write product code or tests.\n` +
      `Worktree: ${worktreeOf(s)}-spec (create from origin/main). Branch: spec/${s.issue}-${s.slug}.\n` +
      (prev.research ? `Base it on the research findings: ${prev.research.findings_path}. Recommended approach: ${prev.research.recommendation}\n` : '') +
      (s.notes ? `Scope notes from the controller: ${s.notes}\n` : '') +
      `Write openspec/changes/<name>/ with proposal.md, design.md, tasks.md and the spec delta. Tasks must be small, ordered, each with the ` +
      `files it touches and a verification command; live checks that can only run on merged main are "- [ ] (post-merge) ...". ` +
      `Both OpenSpec validators must pass. ` +
      `Be brief: this is a hand-off contract, not an essay. Keep every section the validators require, but state decisions, interfaces, failure paths ` +
      `and the test strategy only; link to the research file and the issue, do not restate them. Aim for a proposal under 60 lines and a design under 200. ` +
      `Write all four documents first, THEN run the validators and lint gates once, right before the commit; re-run only what failed. ` +
      `Do NOT run the review script before pushing this design-only pull request (this overrides your usual rule): the workflow runs its own ` +
      `cross-family review of the pushed head straight afterwards and sends you any findings. Push under the shared lock, open the PR titled ` +
      `"docs(openspec): <name> design (Refs #${s.issue})", and comment on the issue. Return status=pr_open with pr_number and head_sha; ` +
      `put the change name in not_done as "openspec_change=<name>; build not started".` +
      FOREGROUND,
    { agentType: 'verdict-builder', label: `spec ${tag(s)}`, phase: 'Spec', schema: BUILD },
  )
  if (!b || b.status !== 'pr_open') return { ...prev, stopped: `spec draft blocked: ${(b && b.blocked_reason) || 'no result'}` }
  // A design PR gets the workflow's own cross-family review too (added 2026-11-24: the author's pre-push review of
  // the #1312 design found nothing with zero tool calls; an independent review of the same head found four defects).
  const pr = b.pr_number
  const specContext =
    `verdict-core issue #${s.issue}, design-only pull request: an OpenSpec change (proposal, design, tasks, spec delta) and no code. ${s.notes || ''} ` +
    `Check that the four documents agree with each other, with the issue and with any operator decisions recorded on it; that every requirement and ` +
    `scenario can be met by the design as written; that the design does not weaken, skip or re-mark an existing test, gate or safety invariant; that ` +
    `every task has a verification command that can fail; that post-merge tasks are live checks only and not build work; and that every claim about ` +
    `the current code, workflow or configuration is true (compare with the source on main).`
  const rounds = []
  for (let round = 0; round <= MAX_FIX_ROUNDS; round++) {
    const rv = await runReview(s, pr, specContext)
    if (!rv || !rv.complete || rv.exit_code !== 0) {
      return { ...prev, build: b, reviews: rounds, stopped: `spec PR #${pr} open, but its independent review is incomplete or missing (fail closed)` }
    }
    rounds.push({ head_sha: rv.head_sha, findings: rv.findings.length, spec_gaps: 0, scenarios: 'n/a', validators_pass: null })
    if (!rv.findings.length) {
      log(`${tag(s)} spec PR #${pr} is open and independently reviewed clean; the build waits for the operator's approval and merge`)
      return {
        ...prev,
        build: { ...b, head_sha: rv.head_sha || b.head_sha },
        reviews: rounds,
        review_clean: true,
        stopped: `spec PR #${pr} open, independent review clean on ${String(rv.head_sha).slice(0, 8)}: operator approval needed, then rerun with stage "build"`,
      }
    }
    if (round === MAX_FIX_ROUNDS) {
      return { ...prev, build: b, reviews: rounds, open_findings: rv.findings, stopped: `spec PR #${pr} open with findings left after the fix budget` }
    }
    const fix = await agent(
      `Fix cross-family review findings on the design-only pull request #${pr} (verdict-core issue #${s.issue}). Documentation only: ` +
        `the OpenSpec change files, no product code, no tests. Worktree ${worktreeOf(s)}-spec, branch spec/${s.issue}-${s.slug}.\n` +
        (s.notes ? `Scope notes from the controller (they still apply): ${s.notes}\n` : '') +
        `Findings (each is a claim to check, not an order: fix the valid ones; report any you dispute with the exact text that contradicts it, never dismiss silently):\n` +
        rv.findings.map((f, i) => `${i + 1}. ${f.path}${f.line ? ':' + f.line : ''} ${f.text}`).join('\n') +
        `\nKeep proposal, design, tasks and the spec delta consistent with each other afterwards. Do not reopen operator decisions. New commit only ` +
        `(no amend, no rebase, no force-push). Both OpenSpec validators must pass: run them once, after all the fixes, right before the commit. ` +
        `Do NOT run the review script yourself for this design-only pull request: the workflow reviews the pushed head again. ` +
        `Fix by changing or removing text; do not add new mechanisms, scripts or sections to answer a finding unless the finding cannot be met otherwise. ` +
        `The PR carries the wip label while build tasks are open, and the pre-push gate applies that allowance only when told: push with ` +
        `VGATE_LINT_WIP=1 under the shared lock, and use no other gate switch. Return status=pr_open with the new head_sha.` +
      FOREGROUND,
      { agentType: 'verdict-builder', label: `spec-fix ${tag(s)} r${round + 1}`, phase: 'Spec', schema: BUILD },
    )
    if (!fix || fix.status !== 'pr_open') {
      return { ...prev, build: b, reviews: rounds, open_findings: rv.findings, stopped: `spec PR #${pr}: fix agent blocked or returned nothing` }
    }
  }
  return prev
}

// The controller builds the context pack itself before the run (verdict-hydrate skill): the pack must not be
// written by the model that implements from it. This stage only checks that the pack exists and its stamp is valid.
const hydrate = async (prev) => {
  if (prev.stopped) return prev
  const s = prev.story
  if (!s.pack_path) {
    return { ...prev, stopped: 'no pack_path: the controller builds the context pack (verdict-hydrate skill) and passes its path' }
  }
  const unit = s.pack_unit || `${HYDRATE_DIR}/issue-${s.issue}.json`
  const h = await agent(
    `Run exactly this one command and report its output. Do nothing else: no edits, and do not rebuild or repair the pack.\n\n` +
      `cd ${REPO} && .venv/bin/python -I .claude/skills/verdict-hydrate/scripts/build_pack.py ${shq(unit)} --repo ${REPO} --check ${shq(s.pack_path)}; ` +
      `echo "EXIT=$?"; wc -c < ${shq(s.pack_path)}\n\n` +
      `Put the number after "EXIT=" in exit_code (99 if that line is missing), everything printed before it in output, and the byte count in bytes.`,
    { label: `pack-check ${tag(s)}`, phase: 'Hydrate', schema: PACK, effort: 'low' },
  )
  if (!h) return { ...prev, stopped: 'pack check returned nothing' }
  if (h.exit_code !== 0) {
    return { ...prev, stopped: `context pack is missing or stale (${String(h.output).slice(0, 200)}): the controller must rebuild it` }
  }
  return { ...prev, pack: { pack_path: s.pack_path, bytes: h.bytes || null, reused: true, gaps: 'see the pack' } }
}

const build = async (prev) => {
  if (prev.stopped) return prev
  const s = prev.story
  const b = await agent(
    `Build verdict-core issue #${s.issue}.\n` +
      `Worktree: ${worktreeOf(s)} (create it from origin/main if it does not exist). Branch: ${branchOf(s)}.\n` +
      `Context pack: ${prev.pack.pack_path}. Read it once and start from it; do not re-explore the repository for what it already contains. ` +
      `If the pack disagrees with the source, the source wins: say so in your report.\n` +
      (s.openspec_change ? `Hand-off contract: build the tasks in openspec/changes/${s.openspec_change}/tasks.md, in order, and tick each only with evidence.\n` : '') +
      (prev.research ? `Research findings to follow: ${prev.research.findings_path}. Recommended approach: ${prev.research.recommendation}\n` : '') +
      (s.notes ? `Scope notes from the controller: ${s.notes}\n` : '') +
      `Follow your instructions end to end: test-first with red-then-green proof, local cross-family review before the first push, ` +
      `push under the shared lock, open the PR with "Refs #${s.issue}", comment on the issue. ` +
      `Return status=pr_open with pr_number and head_sha, or status=blocked with blocked_reason. Always fill not_done honestly.` +
      FOREGROUND,
    { agentType: builderOf(s), label: `build ${tag(s)}`, phase: 'Build', schema: BUILD },
  )
  if (!b) return { ...prev, stopped: 'build agent returned nothing' }
  if (b.status !== 'pr_open' || !b.pr_number) return { ...prev, build: b, stopped: `build blocked: ${b.blocked_reason || 'no PR'}` }
  return { ...prev, build: b }
}

const reviewAndFix = async (prev) => {
  if (prev.stopped) return prev
  const s = prev.story
  const pr = prev.build.pr_number
  const rounds = []
  for (let round = 0; round <= MAX_FIX_ROUNDS; round++) {
    // The cross-family review and the spec check need each other's head, not each other's result: run both, then merge.
    const [rv0, sc] = await parallel([() => runReview(s, pr, reviewContext(s)), () => runSpecCheck(s, pr)])
    // Completeness is read from the script's own summary line, not from the agent's boolean: on
    // 2026-11-25 an agent returned complete:false beside a line that said review=complete files=3/3,
    // and the story stopped with a finding it should have gone on to fix.
    if (rv0 && typeof rv0.raw_first_line === 'string') {
      const m = rv0.raw_first_line.match(/review=(\w+).*files=(\d+)\/(\d+).*tool_failures=(\d+)/)
      rv0.complete = !!m && m[1] === 'complete' && m[2] === m[3] && Number(m[3]) > 0 && m[4] === '0'
    }
    if (!rv0 || !rv0.complete || rv0.exit_code !== 0) {
      // Fail closed: an incomplete or missing review, or a non-zero exit from the review script, is never a pass.
      return { ...prev, reviews: rounds, stopped: 'review incomplete or missing (fail closed)' }
    }
    if (!sc || !sc.checked) {
      // Same rule for the spec check: not run means not verified.
      return { ...prev, reviews: rounds, stopped: 'spec check incomplete or missing (fail closed)' }
    }
    const specFindings = sc.gaps.map((g) => ({ path: g.path, line: '', text: `[spec:${g.kind}] ${g.text}` }))
    const rv = { ...rv0, findings: [...rv0.findings, ...specFindings] }
    rounds.push({
      head_sha: rv0.head_sha,
      findings: rv0.findings.length,
      spec_gaps: sc.gaps.length,
      scenarios: `${sc.scenarios_covered ?? '?'}/${sc.scenarios_total ?? '?'}`,
      validators_pass: sc.validators_pass,
    })
    if (!rv.findings.length) return { ...prev, reviews: rounds, review_clean: true, spec_verified: true }
    if (round === MAX_FIX_ROUNDS) {
      log(`${tag(s)} still has ${rv.findings.length} finding(s) after ${MAX_FIX_ROUNDS} fix rounds; stopping for the controller`)
      return { ...prev, reviews: rounds, open_findings: rv.findings, stopped: 'findings remain after the fix budget' }
    }
    const fix = await agent(
      `Fix cross-family review findings on verdict-core PR #${pr} (issue #${s.issue}), in worktree ${worktreeOf(s)}, branch ${branchOf(s)}.\n` +
        `Context pack (reuse it, do not re-explore): ${prev.pack.pack_path}.\n` +
        (s.notes ? `Scope notes from the controller (they still apply; a finding that asks for out-of-scope work is reported as disputed, not built): ${s.notes}\n` : '') +
        `Findings (each is a claim to check, not an order: fix the valid ones; report any you dispute with the primary source, never dismiss silently):\n` +
        rv.findings.map((f, i) => `${i + 1}. ${f.path}${f.line ? ':' + f.line : ''} ${f.text}`).join('\n') +
        `\nNew commits only (no force-push, no rebase). For each fixed test, show it failing with the fix reverted. ` +
        `Review your new local HEAD with the review script before pushing (through the key wrapper, as your instructions say), push under the shared lock, and return status=pr_open with the new head_sha.` +
      FOREGROUND,
      { agentType: builderOf(s), label: `fix ${tag(s)} r${round + 1}`, phase: 'Fix', schema: BUILD },
    )
    if (!fix || fix.status !== 'pr_open') {
      return { ...prev, reviews: rounds, open_findings: rv.findings, stopped: 'fix agent blocked or returned nothing' }
    }
  }
  return prev
}

const ci = async (prev) => {
  if (prev.stopped) return prev
  const s = prev.story
  const pr = prev.build.pr_number
  let result = await runCi(s, pr)
  if (result && result.state === 'red') {
    const repair = await agent(
      `CI is red on verdict-core PR #${pr} (issue #${s.issue}), worktree ${worktreeOf(s)}, branch ${branchOf(s)}.\n` +
        `Failed checks: ${result.failed_checks.join(', ')}.\n${result.failure_summary || ''}\n` +
        (s.notes ? `Scope notes from the controller (they still apply): ${s.notes}\n` : '') +
        `Find the root cause. If the failure is caused by this change, fix it test-first in a new commit and push under the shared lock. ` +
        `If it also fails on origin/main (check in a detached worktree) or is a load-sensitive timing test, do not change the test or any limit: ` +
        `return status=blocked with the evidence.` +
      FOREGROUND,
      { agentType: builderOf(s), label: `ci-repair ${tag(s)}`, phase: 'CI', schema: BUILD },
    )
    if (repair && repair.status === 'pr_open') {
      // A new head needs a new review as well as new CI.
      const [rv, sc] = await parallel([() => runReview(s, pr, reviewContext(s)), () => runSpecCheck(s, pr)])
      if (!rv || !rv.complete || rv.exit_code !== 0 || rv.findings.length || !sc || !sc.checked || sc.gaps.length) {
        return { ...prev, ci: result, stopped: 'CI repair pushed a head that is not cleanly reviewed and spec-checked' }
      }
      result = await runCi(s, pr)
    } else {
      return { ...prev, ci: result, stopped: `CI red, repair blocked: ${(repair && repair.blocked_reason) || 'no result'}` }
    }
  }
  if (!result || result.state !== 'green') {
    return { ...prev, ci: result, stopped: `CI not green: ${result ? result.state : 'no result'}` }
  }
  // Ready only when the independent review record and the green check runs are on one commit: the PR's current head.
  const v = await agent(
    `Run exactly this one command from ${REPO} and report its output. Do nothing else.\n\n` +
      `REVIEW_AUTHOR_FAMILY=${AUTHOR_FAMILY} REVIEW_RECORD_SUFFIX=${REVIEW_SUFFIX} ${VERIFY} ${pr}; echo "EXIT=$?"\n\n` +
      `It prints one line starting "READY" or "NOT READY", then "EXIT=<code>". Put that line, unchanged, in line; the number in exit_code ` +
      `(99 if the EXIT line is missing); and the 40-character value after "head=" in head_sha.`,
    { label: `verify ${tag(s)}`, phase: 'CI', schema: READY, effort: 'low' },
  )
  if (!v || v.exit_code !== 0 || !String(v.line).startsWith('READY ')) {
    return { ...prev, ci: result, verify: v, stopped: `readiness check failed: ${v ? String(v.line).slice(0, 300) : 'no result'}` }
  }
  return { ...prev, ci: { ...result, head_sha: v.head_sha || result.head_sha }, verify: v, ready: true }
}

// ---------------------------------------------------------------- run
const out = []
for (let i = 0; i < stories.length; i += MAX_PARALLEL) {
  const chunk = stories.slice(i, i + MAX_PARALLEL)
  log(`stories ${chunk.map(tag).join(', ')} (${i + chunk.length}/${stories.length})`)
  const done = await pipeline(chunk, research, spec, hydrate, build, reviewAndFix, ci)
  done.forEach((r, k) => out.push(r || { story: chunk[k], stopped: 'a stage threw; see the workflow journal' }))
}

// ---------------------------------------------------------------- learning loop
// One record per story goes to a ledger; lessons are updated only from defects that recur.
const summary = out.map((r) => ({
  issue: r.story.issue,
  research: Boolean(r.research),
  stage: r.story.stage || 'build',
  size: r.story.size || null,
  builder: builderOf(r.story),
  pack_built_by: 'controller',
  pack_bytes: r.pack ? r.pack.bytes || null : null,
  verify: r.verify ? r.verify.line : null,
  pr: r.build ? r.build.pr_number || null : null,
  ready: Boolean(r.ready),
  stopped: r.stopped || null,
  review_rounds: r.reviews || [],
  open_findings: (r.open_findings || []).map((f) => f.text.slice(0, 200)),
}))
const RETRO = {
  type: 'object',
  properties: {
    ledger_lines_appended: { type: 'integer' },
    lessons_added: { type: 'array', items: { type: 'string' } },
    lessons_retired: { type: 'array', items: { type: 'string' } },
    proposals: { type: 'array', items: { type: 'string' } },
    retro_path: { type: 'string' },
  },
  required: ['ledger_lines_appended', 'lessons_added', 'lessons_retired', 'proposals'],
}
const retro = await agent(
  `Record this story-pipeline run and propose lesson changes. You may write ONLY these two paths, both under ${REPO}/.claude/: ` +
    `pipeline-ledger.jsonl (append) and pipeline-retro/<UTC date and time>.md (create). Touch nothing else: ` +
    `pipeline-lessons.md is a versioned file that changes only through a pull request, so read it but do not edit it; ` +
    `no agent definitions, no workflow, no hooks, no settings, no repository files, no git commands that change state.\n\n` +
    `Run summary (JSON):\n${JSON.stringify(summary)}\n\n` +
    `1. Ledger: for each story append one JSON line to pipeline-ledger.jsonl with: utc time (from \`date -u +%FT%TZ\`), issue, pr, stage, research, ` +
    `pack_bytes, ready, stopped, number of review rounds, findings and spec gaps per round, first_round_clean (true when round 1 had 0 findings and 0 spec gaps), ` +
    `and finding_kinds: for every finding in that PR's stored reviews (/tmp/claude-takeover/reviews/<sha>.independent.json and, when present, the author's pre-push record <sha>.json, for each head_sha in review_rounds, field "comments") ` +
    `and every "[spec:<kind>]" gap, one short kebab-case kind such as test-cannot-fail, docs-ahead-of-code, missing-null-reason, fail-open-input, ` +
    `cleanup-on-normal-exit. Reuse kinds already in the ledger before inventing a new one.\n` +
    `2. Trend: from the whole ledger compute the share of stories with first_round_clean, the mean review rounds per PR, and the five most frequent finding kinds, ` +
    `for the last 10 stories and for all stories. Say plainly when there are too few stories to compare.\n` +
    `3. Lessons (proposed changes to pipeline-lessons.md, which stays at most 15 entries, one or two sentences each, with the PR numbers as evidence; write the proposed entries in the retro file and list them in lessons_added and lessons_retired): propose a new lesson only when a kind has now appeared ` +
    `in at least two different stories and no existing lesson covers it; propose rewording an existing lesson when a new case shows it was too narrow; propose retiring a lesson whose kind has ` +
    `not appeared in the last ten ledger stories (keep the retired text in the retro file). Never propose a lesson from a single occurrence, never weaken or remove a rule ` +
    `because it was inconvenient, and never write anything that changes a gate, a limit or a review requirement.\n` +
    `4. Proposals (do not apply them): when a kind that already has a lesson appears again in two or more later stories, the lesson is not working: propose turning it ` +
    `into something enforced (a hook, a test, a spec-check rule, a line in the builder agent), with the evidence. Also propose a change when the trend got worse after a ` +
    `recorded process change. These are for the controller and the operator to decide.\n` +
    `5. Write pipeline-retro/<date-time>.md: the trend table, what you added or retired and why, and the proposals. Return the structured result.`,
  { label: 'retro', phase: 'Retro', schema: RETRO, effort: 'medium' },
)
if (retro && retro.proposals.length) log(`retro: ${retro.proposals.length} proposal(s) for the controller, see ${retro.retro_path || '.claude/pipeline-retro/'}`)

// The workflow never merges. "ready" means: the workflow's own clean cross-family review and a clean spec check on the exact head,
// green CI, and verify_ready.sh confirming the review record and the check runs are on the same commit.
return { retro: retro || { error: 'retro did not run' }, stories: out.map((r) => ({
  issue: r.story.issue,
  ready_for_merge_decision: Boolean(r.ready),
  spec_verified: Boolean(r.spec_verified),
  pr: r.build ? r.build.pr_number : null,
  head_sha: (r.ci && r.ci.head_sha) || (r.build && r.build.head_sha) || null,
  review_rounds: r.reviews || [],
  stopped: r.stopped || null,
  open_findings: r.open_findings || [],
  research: r.research ? r.research.findings_path : null,
  pack: r.pack ? r.pack.pack_path : null,
  not_done: r.build ? r.build.not_done : null,
  verify: r.verify ? r.verify.line : null,
})) }
