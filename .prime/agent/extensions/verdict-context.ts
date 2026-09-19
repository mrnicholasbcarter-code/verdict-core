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
  let pending = false;
  let cooldown = 0;

  // Brand hook → semantic event: before_compact
  pi.on('session_before_compact', async (event) => {
    const preparation = event && event.preparation;
    if (!preparation) return;
    const operatorInstructions = event.customInstructions
      ? `\nOperator instructions: ${event.customInstructions}` : '';
    return {
      compaction: {
        summary: `${CONTINUITY}${operatorInstructions}\n`
          + 'The authoritative checkpoint is the common-git verdict-prime/checkpoint.json; '
          + 'reload it in a fresh process before taking the next action.',
        firstKeptEntryId: preparation.firstKeptEntryId,
        tokensBefore: preparation.tokensBefore,
        details: {
          source: 'verdict-core/.prime/agent/extensions/verdict-context.ts',
          semantic_event: 'before_compact',
          contract: 'verdict.compaction',
          checkpoint: 'git-common-dir/verdict-prime/checkpoint.json',
          bounded: true,
        },
      },
    };
  });

  // Brand hook → semantic event: context_pressure_checkpoint (not timer alone)
  pi.on('turn_end', async (_event, ctx) => {
    if (pending || cooldown-- > 0) return;
    const usage = ctx.getContextUsage();
    if (!usage || usage.tokens < 20000) return;
    if (usage.tokens < 120000 && !(usage.percent >= 60)) return;
    pending = true;
    const release = () => { pending = false; cooldown = 5; };
    try {
      ctx.compact({
        customInstructions: CONTINUITY,
        onComplete: release,
        onError: (error) => {
          release();
          ctx.ui?.notify?.(`Verdict compaction failed: ${error.message}`, 'warning');
        },
      });
    } catch (error) {
      release();
      ctx.ui?.notify?.(`Verdict compaction failed: ${error.message}`, 'warning');
    }
  });
}
