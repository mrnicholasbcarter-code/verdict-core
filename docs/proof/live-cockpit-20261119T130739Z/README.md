# Live orchestrate run 20261119T130739Z (evidence record)

**LIVE, not a fixture.** One real `verdict orchestrate` run against the
operator's local OmniRoute gateway on 2026-11-19, from a clean checkout of
`main` `35c110c10709c04c6e261b943e8c9371a2434c0b` (Verdict 1.0.0). Every
worker, planner and reviewer call was live, on FREE capacity only
(`--capacity free`). No fault was injected (`fault_injected: false` on every
attempt).

**Raw run directory retained privately; digests recorded here.** `events.jsonl`
is digest-bound (the receipt pins its SHA-256) and contains absolute host paths
(the temporary repository and run directories), so it can only be committed
byte for byte and the push gate rejects host paths under `docs/proof/`. As in
[live-run-20261119T000727Z](../live-run-20261119T000727Z/README.md), this
directory is an evidence record, not a verifiable bundle: `verdict run-receipt`
cannot be run on it. The verification below was run on a copy of the private
run directory.

## Run

| Item | Value |
| --- | --- |
| Run id | `20261119T130739Z` |
| Source SHA (producer, clean tree) | `35c110c10709c04c6e261b943e8c9371a2434c0b` |
| Goal | Add `textkit/slug.py` with `slugify(text)` and `textkit/words.py` with `word_count(text)`, each with its own pytest file; the full suite must pass |
| Window | 2026-11-19T13:07:40Z to 13:11:20Z |
| Outcome | `COMPLETE`: all implement/integrate nodes validated, barrier ok, review PASS |
| Planner | `cerebras/gpt-oss-120b` (free) |
| `init_textkit` | 1 attempt: `cerebras/gpt-oss-120b` success |
| `words_implement` | `cerebras/gpt-oss-120b` failed with a real HTTP 429 from the provider ("Tokens per minute limit exceeded", retry after 59 s); the `cerebras` pool was put on cooldown until 13:08:50 and the node was reassigned to `nvidia/google/gemma-4-31b-it`, which succeeded |
| `slug_implement` | `cerebras/gpt-oss-120b` wrote code whose own test failed (`--Already--slug` gave `already--slug`, expected `already-slug`); classified `verification_failed` and reassigned to `nvidia/google/gemma-4-31b-it`, which passed |
| `integrate_all` | merge of the validated commits; full `pytest -q` exit 0; ownership and integration barriers ok |
| Reviewer | open-code-review v1.12.9 on `kc/nvidia/nemotron-3-ultra-550b-a55b:free`, PASS, 0 blocking (2 non-blocking findings), 33.6 s; both implementer routes excluded (family-level independence) |
| Events digest | `sha256:df71f19e845110dd41b8a544ac2e18206d6b411bb54de5a7103f246e100e1eef` (100 events) |
| Graph digest | `sha256:6e0376497c737bd38226840af5381b9a0f9108a6c2916de4536d997e842e70a7` |

Command (paths neutralised):

```bash
verdict orchestrate "<goal above> PARALLEL_WORK_UNITS 3" --repo <temporary repo> \
  --runs-dir <runs> --gateway http://127.0.0.1:20128 --executor direct-gateway \
  --capacity free --max-attempts-per-node 4 --max-parallel 3 \
  --attempt-timeout 300 --run-deadline 1500 --json
```

It exited 0.

`verdict run-receipt` on a copy of the private run directory, 2026-11-19
around 13:30Z, exit 0:

```text
COMPLETE: all implement/integrate nodes validated, barrier ok, review PASS
integrity: OK (events digest verified)
  init_textkit       VALIDATED        cerebras/gpt-oss-120b[success]
  slug_implement     VALIDATED        cerebras/gpt-oss-120b[failure:verification_failed] -> nvidia/google/gemma-4-31b-it[success]
  words_implement    VALIDATED        cerebras/gpt-oss-120b[failure:rate_limited] -> nvidia/google/gemma-4-31b-it[success]
  integrate_all      VALIDATED        merge[success]
  review: PASS by open-code-review v1.12.9 on kc/nvidia/nemotron-3-ultra-550b-a55b:free
```

## Private files (sha256)

| File in the run directory | sha256 |
| --- | --- |
| `events.jsonl` | `df71f19e845110dd41b8a544ac2e18206d6b411bb54de5a7103f246e100e1eef` |
| `receipt.json` | `ee353dad864a977d7b2953953abdf707b1b876524d2b29a485699737c35362ab` |
| `graph.json` | `33a3b4475f69ceec9c827a1b97320437b2937a37459996fa5ff10435d7f672ec` |
| `progress.json` | `bda60cdd6301811d7ca6cd56b444870f0053cd557bbd06fd033c903bb86ac0c5` |
| `outcome-records.jsonl` | `cfb13be4b69e9cff6dd0805073089bbfd12eb46ec7e5be88de25adcc6c6d885a` |

## Limits

- One run. No benchmark, speed, cost or savings claim is made from it.
- It ran inside the certification rehearsal harness with existing route-health
  state; it is not a cold-start run.
- The cockpit's CONTROLLER panel lists the routes of planner and worker calls;
  the root controller was a bare launch (root-pinned, no automatic failover),
  as the cockpit header says.
- The replay label reads "mixed executor provenance: marked and unmarked
  terminals" because the merge node `integrate_all` has no model executor; all
  five model attempts are marked `live`.

## Demo recording

[`docs/assets/live-cockpit.svg`](../../assets/live-cockpit.svg) (animated),
[`live-cockpit-poster.svg`](../../assets/live-cockpit-poster.svg) (still of
the COMPLETE cockpit) and the asciicast
[`live-cockpit.cast`](../../assets/live-cockpit.cast) are a real PTY capture,
132 x 68, of `verdict watch 20261119T130739Z --replay --speed 0.5` on the copy,
followed by `verdict run-receipt 20261119T130739Z` (exit 0). About 45 s long.
The replay caps each gap between events at 1.5 s of run time (the run took
220 s; the replay plays the capped timeline at half speed). Nothing in the
frames was edited: the capture was taken with the run's parent directory as the
working directory, so only the run id appears. The final frame is held for
4 s. Regenerate with the commands in
[Regenerating the demo assets](../../guides/offline-demo.md#regenerating-the-demo-assets).
