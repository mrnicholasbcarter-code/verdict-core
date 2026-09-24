// Plain JavaScript syntax keeps the policy directly testable with Node's test runner.
// Maps Prime `session_before_compact` → harness-neutral semantic event `before_compact`.
// Canonical contract: verdict.compaction (BOD-69 / Continuity C03). Not a second summarizer.
const CONTINUITY = 'Preserve the current issue, exact worktree/base/head, attempt and target, '
  + 'acceptance criteria, decisions/ADRs, completed checks and failures, blockers, lease generation, '
  + 'and ONE next action. Preserve the active program/goal, completed issue/PR/merge SHA, '
  + 'current/next Linear story, remaining proof requirements, durable architectural decisions, '
  + 'and active worktree/branch/PR mappings. Preserve the common-git verdict-prime/checkpoint.json '
  + 'location and the packet/receipt/proof paths. After compaction reload that durable checkpoint '
  + 'and reconcile Linear/git/GitHub; summaries are not authority. Keep source references rather '
  + 'than repeating source dumps. Discard shell chatter, duplicate MCP output, already-incorporated '
  + 'searches, verbose worker transcripts, repeated tool output, and abandoned approaches except as '
  + 'a one-line constraint. Semantic events: before_compact, before_yield, session_end, resume, '
  + 'context_pressure_checkpoint.';

export default function verdictContext(pi) {
  let cooldown = 0;
  // Prime's ctx.compact() aborts even from agent_end (activeRun can still exist).
  // Use the native scheduler for compaction, never an extension lifecycle callback.
  pi.on('agent_end', async (_event, ctx) => {
    if (cooldown-- > 0) return;
    const usage = ctx.getContextUsage();
    if (!usage || usage.tokens < 20000) return;
    if (usage.tokens < 120000 && !(usage.percent >= 60)) return;
    cooldown = 5;
    pi.appendEntry('verdict_context_checkpoint', {
      instructions: CONTINUITY,
      tokens: usage.tokens,
      next_action: 'Persist active operation evidence; use native safe-boundary compaction.',
    });
  });
}
