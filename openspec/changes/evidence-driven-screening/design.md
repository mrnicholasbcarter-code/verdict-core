# Design — screening that learns, and proof from real work

## Context

`verdict identify --screen` runs every six hours from a timer (`--allow-live-probe --capacity free,subscription --max-per-provider 16 --max-routes 40`). It builds a queue of about 645 rows, skips rows that are fresh, negative, aliased, in a blocked bucket or over a cap, and sends the rest a three-turn tool conversation. A pass is stored in the health cache as agentic evidence valid for 24 hours; the eligibility ladder requires it for a worker node. Passive identification (`--passive`) reads seven gateway sources and decides which routes are passively admitted; that result is not what orders the screen.

This design was rewritten on 2026-11-27 after a review of all OmniRoute 3.8.55 docs and a backtest of every proposed signal against the day's 120 real probe results. The first draft ordered the queue by a weighted score over seven new gateway reads. The backtest showed that three plain rules explain the results and that most of the reads add nothing, so the score and six of the seven reads are gone.

## Goals / Non-Goals

Goals:

- Stop spending probes on a provider that never passes.
- Probe first where a pass is likely, using facts Verdict already holds.
- Let real, verified work keep and create proofs, so probing is for routes with no work history.
- Make the size and trend of the proven pool visible.

Non-Goals:

- Replacing the tool proof with passive evidence. Nothing here does.
- A weighted score, or readers for the gateway's quality state, model-health, latency statistics, provider rankings or the community roster. Measured below: they do not separate passing routes from failing ones, or they repeat what Verdict's own record says.
- A new timer or a long-running service. The existing timer and its lock stay.
- A proof shared between two ids of one model. A proof stays with the exact id that earned it (ADR-010).

## Decisions

1. **One probe per model, not per id.** The gateway lists most models under two ids: the provider's short alias and its full id (`ddgw/x` and `duckduckgo-web/x`), with the mirror's `parent` naming the other. The queue is keyed on the catalog's `owned_by` and `root`. For each model the screen probes one id, chosen by a fixed rule so it is the same id in every run, and it does not probe a model while either of its ids holds a fresh proof. The proof still belongs only to the id that earned it; the other id stays unproven. Today a model is probed once in a run, but under different ids in different runs (3 of 96 models on 2026-11-27). Every count in the report is per model.
2. **A provider that never passes is backed off.** The record is kept per provider (`owned_by`), over seven days. A provider with at least 8 probes and no pass is skipped for 24 hours, then given one probe; a pass ends the back-off. Skips are reported as `skipped_backoff`. What counts as a probe: a pass, a tool failure, and the provider answering that it does not serve the model (the `catalog_stale` and `not_found` results; 47 of the 74 free failures on 2026-11-27 were this, on the first request). What does not count: a refusal by the gateway before any upstream call (open breaker, connection cooldown, model lockout, `concurrency_limited`, `resource_pressure`), a rate limit, a timeout and an upstream 5xx. The report shows each provider's results by kind. The numbers are settings.
3. **A route that does not declare tool support is not probed.** It is reported as `skipped_no_tools`.
4. **Order by three tiers, then by the provider's pass rate.**
   - First: routes the passive check admits (a live candidate row, breaker closed, connection usable).
   - Second: other routes of a provider with at least one pass in seven days.
   - Third: the rest, including providers with no record yet.
   - Inside a tier: the provider's pass rate, then Verdict's rating where one exists.
   - One probe in eight goes to the third tier whenever it is not empty, so a new provider gets a record.
   - A rescreen hint makes a route a candidate and no longer moves it ahead.
5. **Unknown capacity is screened when the passive check admits it.** The timer's capacity list gains `unknown`, and an unknown-capacity route is a candidate only in the first tier. That is the 142 routes the operator agreed to on 2026-11-27. The whole class is 1,823 ids (1,130 models), most of them on connections the gateway marks expired; they are not candidates. ADR-049 already asks for the same proof.
6. **The operator's deny list excludes; a terms rating does not.** A provider on the deny list is not probed and is reported as `skipped_denied`. The list is keyed by provider id, is empty unless the operator fills it, and no provider name is written into code. The gateway's free catalog also rates each provider's terms (`ok`, `caution`, `avoid`); the report shows that rating beside each provider and nothing acts on it. This is the only new gateway read (`/api/free-models`).
7. **Account-tied providers are probed sparingly.** A screen probe is an ordinary request to the gateway, and the gateway's ban detection treats it as one. The screen addresses routes, not connections, so the limit is per provider: for a provider whose connections sign in as a person (`authType` `oauth` or `cookie` in the connection rows Verdict already reads to classify capacity), the screen sends at most two probes per run, and none while none of its connections is usable. Decision 8 is what keeps those routes proven. Whether to probe them at all is the operator's decision (open question 1).
8. **Proof from real work.** After a node is VALIDATED, the runtime records session-grade tool evidence for the attempt's exact route when all of these hold: the node required tools; the executor is a live harness worker (not fault-injected, not scripted); the served model was verified against the assigned route; the run's verification command passed. It uses the kind the manual import already accepts for real tasks, so the lifetime (seven days) and the ladder's reading of it are unchanged. A failed or unverified task records what it records today.
9. **Report.** Each screen run and `verdict doctor` show: models holding a tool proof by capacity class, the unproven backlog, passes over probes per provider for seven days with its terms rating, and the providers in back-off.

## What was measured

Three screen runs on 2026-11-27 (06:20, 12:20, 18:20 UTC; 120 probes), joined to read-only gateway reads of the same day. Free routes: 10 passes in 84 probes.

| Signal | With it | Without it | Used |
|---|---|---|---|
| Passively admitted in the same run (the two never-passing providers left out) | 9 of 17 | 1 of 25 | yes, tier 1 |
| Passively admitted, the two never-passing providers | 0 of 13 | 0 of 29 | this is why back-off comes first |
| Provider has passed before (`openrouter`, `bazaarlink`, `nvidia`) | 10 of 23 | 0 of 61 | yes, tier 2 and the order |
| Declares tool support | 10 of 66 | 0 of 18 | yes, a filter |
| Candidate row says `reachable` | 6 of 27 | 1 of 3 | no |
| Model is in the gateway's free catalog | 1 of 26 | 9 of 58 | no |
| Model has a rating | 1 of 11 | 9 of 73 | tie-break only |
| Gateway success rate for the provider | `duckduckgo-web`: 666 of 785 requests succeeded, 0 of 15 probes passed | | no |

The provider row is computed on the same probes it describes, so it shows that providers differ, not how well yesterday predicts today. Task 5.1 measures that.

What the 74 free failures were: 47 times the provider answered that it does not serve the model (HTTP 400 on the first request), 12 times the model answered and did not use the tool, 4 not found, 5 other HTTP 400 after a first answer, 3 upstream 502, 3 timeouts. A failed route is retried after 6 to 24 hours today, which is how one provider's stale catalog takes nine probes of every run.

The gateway's traffic and quality figures are mostly Verdict's own discovery requests and probes read back, and a chat answer is not a tool call.

Size of the work: 559 distinct free models are in the queue and 529 declare tools. 375 of those belong to one provider that passed 0 of 27. 154 remain, which is one day of probes at the current budget. 36 of the 120 probes went to subscription routes that real work uses daily.

## Risks / Trade-offs

- A provider that has just been fixed waits up to a day for its one probe.
- Tier 2 can crowd out tier 3 while a passing provider has a long list (`nvidia` has 71 unproven models). The one-in-eight share for tier 3 bounds that.
- Proof from real work only helps routes that get work. Free routes get work only once they hold a proof, so the screen still has to start the cycle.
- A task can pass verification without the model having driven the tools well. The conditions in decision 8 narrow that; they do not remove it. The proof it grants is the same strength as an imported session.
- The gateway can answer from another model when a budget is exhausted (`OMNIROUTE_EMERGENCY_FALLBACK`, on by default, per its docs). The probe already checks the served model; task 0.2 confirms on the live gateway that this check sees such an answer before any of this is built.
- The record is per provider, so one provider with good and bad models is judged as a whole. `api-airforce` is the case this is for: 375 models, none passing.

## Security / trust implications

Every input is gateway metadata or Verdict's own stored results and is treated as untrusted: it can order or remove candidates and cannot admit, authorise or mark a route healthy. Proof from real work is written only by the controller, from its own verification result and its own served-model check, never from worker text. Fewer probes reach account-tied connections than today. No credential is read or stored beyond the existing gateway key.

## Routing / context / memory implications

Routing rules do not change: cheapest qualified tier first, tool proof before dispatch. More free routes should qualify, which is what lets the first rule choose them. Nothing is written to memory.

## Migration / rollback

The health cache keeps its schema. The per-provider record and back-off state are derived from stored screen results or kept in a small new file beside it. The timer's arguments change through `verdict setup`. Reverting restores the current queue; proofs already written by real work expire by their own lifetime.

## ADR impact

ADR-049: no change (unknown capacity already needs the same proof). ADR-010 and ADR-014 are kept: no claim, HTTP success or catalog flag becomes capability. A short ADR note is needed for decision 8, because it changes when a session outcome may widen what a route can do.

## Open questions for the operator

1. May the screen probe account-tied connections at all? Recommended: yes, within the limit in decision 7.
2. Does `duckduckgo-web` go on the deny list? Recommended: yes. Its terms forbid automated querying and it passed 0 of 15.
