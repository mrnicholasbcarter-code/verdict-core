# Design — screening that learns, and proof from real work

## Context

`verdict identify --screen` runs every six hours from a timer (`--allow-live-probe --capacity free,subscription --max-per-provider 16 --max-routes 40`). It builds a queue of about 645 rows, skips rows that are fresh, negative, aliased or over a cap, and sends the rest a three-turn tool conversation. A pass is stored in the health cache as agentic evidence valid for 24 hours; the eligibility ladder requires it for a worker node. Passive identification (`--passive`) reads seven gateway sources and decides which routes are passively admitted; that result is not what orders the screen. A second timer, the discovery registry refresh, probes free routes with one tool request and feeds the ladder through a different record.

## Goals / Non-Goals

Goals:

- Spend each probe where it is most likely to add a usable route.
- Use the evidence the gateway and the roster already give, without one more model call.
- Let real, verified work keep and create proofs, so probing is for routes with no work history.
- Make the size and trend of the proven pool visible.

Non-Goals:

- Replacing the tool proof with passive evidence. Nothing here does.
- A long-running qualifier service or a new timer. The existing timer and its coordinator lock stay; only what it probes and in what order changes.
- Merging the screen with the discovery registry refresh. They keep two records for now.
- Proof shared across alias twins of one backend model (ADR-010 against the credential-pool view). It needs its own decision.

## Decisions

1. **Candidates come from passive admission.** A free or unknown-capacity route is a candidate only when the passive check admits it (live candidate row, breaker closed, connection usable). Subscription routes in the planner's frontier band stay candidates even outside that pool: 11 of 27 such probes passed, and that case is why the inventory branch exists (#1284).
2. **Expected value orders the queue.** Score = estimated pass chance times quality.
   - Pass chance starts from the prefix's own record over the last seven days of screen results (passes over probes). A thin record is pulled toward the overall rate in the way the gateway's own quality tracker does it: confidence is samples over 50, capped at 1, and the estimate is the neutral value plus confidence times the difference. A prefix with no record is neither favoured nor penalised.
   - It is then raised or lowered by passive signals for the route: it answered in the latest discovery registry refresh (the cheap liveness pass Verdict already runs over every free route); the gateway's per-model quality state from real traffic (`/v1/explain/routing`: success average, sample count, recency); healthy in the gateway's model-health; tool support declared in the catalog; membership of the gateway's `best-free` or `coding:free` pool; presence on the `free-coding-models` roster.
   - Quality is Verdict's rating for the model where one exists; otherwise the gateway's free-provider ranking score for the provider; otherwise the lowest band.
   - A rescreen hint no longer puts a route ahead of a better-scored one; it only makes the route a candidate.
3. **Back-off from results.** A prefix with at least 8 probes and no pass in seven days is skipped for 24 hours, then given one probe; a pass ends the back-off. Skips are reported as `skipped_backoff`. The numbers are a starting point and are settings.
4. **Unknown capacity is screened.** The timer's capacity list gains `unknown`. ADR-049 already asks for the same proof.
5. **Terms of use are reported, and the operator decides.** The gateway's free catalog rates each provider's terms (`ok`, `caution`, `avoid`). The gateway's own reference says the flag "is advisory, not a routing gate", and `avoid` currently covers providers the operator uses on purpose (`agy`, the Antigravity sign-in, and `kiro`) as well as `duckduckgo-web`, whose terms forbid automated querying. So the rating never excludes a route by itself: the screen shows it next to each prefix, and an operator-owned deny list (empty unless the operator fills it) is what excludes a provider, reported as `skipped_denied`. No provider name is written into the code. `cloudflare-playground` is not in the catalog.
6. **New readers are read-only and bounded.** One GET each for model-health, the routing quality state, model-latency stats, the capability-filtered catalog, the `best-free` candidate channel, the free catalog and the provider rankings, with the same size and time bounds and fail-closed parsing as the existing seven sources. A failed reader removes its signal from the score; it never blocks the screen and never counts in a route's favour.
7. **Proof from real work.** After a node is VALIDATED, the runtime records session-grade tool evidence for the attempt's exact route when all of these hold: the node required tools; the executor is a live harness worker (not fault-injected, not scripted); the served model was verified against the assigned route; the run's verification command passed. It uses the kind the manual import already accepts for real tasks, so the lifetime (seven days) and the ladder's reading of it are unchanged. A failed or unverified task records what it records today.
8. **Report.** Each screen run and `verdict doctor` show: routes holding a tool proof by capacity class, the admitted-unproven backlog, and passes over probes per prefix for seven days.

## Risks / Trade-offs

- The pass-chance estimate can starve a prefix that has just been fixed. The single probe after each back-off period bounds that at a day.
- Proof from real work only helps routes that get work. Free routes get work only once they hold a proof, so the screen still has to start the cycle; this change makes it start with the likeliest routes.
- A task can pass verification without the model having driven the tools well. The conditions in decision 7 narrow that; they do not remove it. The proof it grants is the same strength as an imported session, not stronger.
- Seven more gateway reads per run. They are management reads with no inference; the gateway review found two of them slow to answer under load.
- Reporting the terms rating without acting on it leaves a provider whose terms forbid this use in the queue until the operator denies it. That is the operator's choice to make, and the report puts it in front of them on every run.
- Speed is not the constraint. A one-token "hi" to every route in parallel, which is how `free-coding-models` checks hundreds of models in seconds, is what the discovery registry refresh already does: about 1,100 free routes in two to three minutes, of which 26 to 37 answered and 1 or 2 passed the tool request in each of the last three good runs. Most free routes on this gateway do not answer at all.

## Security / trust implications

Every new input is gateway metadata or a public roster and is treated as untrusted: it can order or remove candidates and cannot admit, authorise or mark a route healthy. Proof from real work is written only by the controller, from its own verification result and its own served-model check, never from worker text. No credential is read or stored by the new readers beyond the existing gateway key.

## Routing / context / memory implications

Routing rules do not change: cheapest qualified tier first, tool proof before dispatch. More free routes should qualify, which is what lets the first rule choose them. Nothing is written to memory.

## Migration / rollback

The health cache keeps its schema; back-off state and the per-prefix record are derived from stored screen results or kept in a small new file beside it. The timer's arguments change through `verdict setup`. Reverting restores the current queue; proofs already written by real work expire by their own lifetime.

## ADR impact

ADR-049: no change (unknown capacity already needs the same proof). ADR-010 and ADR-014 are kept: no claim, HTTP success or catalog flag becomes capability. A short ADR note is needed for decision 7, because it changes when a session outcome may widen what a route can do.
