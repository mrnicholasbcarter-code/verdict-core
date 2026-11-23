# Spec Delta: Coordinator context forwarding

## ADDED Requirements

### Requirement: A validated node SHALL write a durable results artifact to run_dir

Each successfully verified node SHALL write `run_dir/results/{node_id}.json` with the
full verified output (capped to 32768 bytes), the git HEAD SHA at VALIDATED, the node id,
and a validated-at timestamp. Redaction SHALL be applied before writing.

An implement node SHALL also write `run_dir/results/{node_id}-diff.patch` containing the
git diff of `node.owned_files` relative to the node's merge base, bounded to 8192 bytes,
with redaction applied.


#### Scenario: Results artifact written at VALIDATED

- **GIVEN** a node whose verification command passes
- **WHEN** `_set(run, NodeState.VALIDATED)` is about to fire
- **THEN** `run_dir/results/{node_id}.json` exists and contains `output`, `git_head`, `node_id`, `validated_at`
- **AND** `output` does not contain text matching `_secret_assignment_re`

#### Scenario: Implement node also writes diff artifact

- **GIVEN** an implement node that becomes VALIDATED
- **WHEN** `run_dir/results/` is checked
- **THEN** `run_dir/results/{node_id}-diff.patch` exists and is at most 8192 bytes

#### Scenario: Absent or unreadable results artifact does not block downstream node

- **GIVEN** a node whose predecessor's results artifact is missing
- **WHEN** `hydrate_with_research_pack` is called for the downstream node
- **THEN** the downstream node's pack is built without the upstream unit
- **AND** an omission note appears in `WorkerContext.omissions`
- **AND** the downstream node is NOT blocked

---

### Requirement: Dependent node packs SHALL include upstream verified results as cited units

`hydrate_with_research_pack` SHALL load predecessor results artifacts for each node in
`node.depends_on`. For each loaded predecessor, it SHALL build a `ContextUnit` with:
- `key="upstream:{pred_id}"`
- `slot_type="evidence"`
- `source_uri="urn:verdict:run:{run_id}:{pred_id}:validated"`
- `source_digest=sha256(output_text)`
- `content`: the full output for research predecessors; the diff patch for implement predecessors.


#### Scenario: Research predecessor output reaches dependent pack

- **GIVEN** a research node R has VALIDATED with output text T
- **AND** an implement node I has `depends_on=[R.node_id]`
- **WHEN** `hydrate_with_research_pack` is called for I
- **THEN** I's pack includes a `ContextUnit` with `key="upstream:{R.node_id}"` and `content` including text T
- **AND** the unit's `source_uri` is `"urn:verdict:run:{run_id}:{R.node_id}:validated"`

#### Scenario: Implement predecessor diff reaches dependent pack

- **GIVEN** an implement node P has VALIDATED and `run_dir/results/{P.node_id}-diff.patch` exists
- **AND** an integrate node Q has `depends_on=[P.node_id]`
- **WHEN** `hydrate_with_research_pack` is called for Q
- **THEN** Q's pack includes a `ContextUnit` with `key="upstream:{P.node_id}"` and content drawn from the diff patch

#### Scenario: No frontier call is added per node

- **GIVEN** any node being hydrated
- **WHEN** upstream forwarding is active
- **THEN** `hydrate_with_research_pack` makes no model API call
- **AND** no additional gateway request is recorded in `events.jsonl` before the dispatch event

---

### Requirement: Every pack in a run SHALL include a shared core

Every call to `hydrate_with_research_pack` SHALL inject a `ContextUnit` with
`key="required:shared-core"`, `slot_type="instructions"`, bounded to 1500 estimated tokens,
containing the run goal, the constraints text (if non-empty), and the ADR/decision ids
drawn from `node.required_context`. The goal item SHALL carry
`source_uri="urn:verdict:run:{run_dir.name}:goal"`.

If the constraints text is absent, the shared core SHALL include a note of the omission
rather than failing.


#### Scenario: Shared core present in every pack

- **GIVEN** `hydrate_with_research_pack` is called with a non-empty goal
- **WHEN** the returned `WorkerPrompt` is examined
- **THEN** the compiled prompt includes text from the `required:shared-core` unit
- **AND** the unit is not truncated before units with lower-priority keys

#### Scenario: Shared core absent when goal is empty

- **GIVEN** `hydrate_with_research_pack` is called with goal=""
- **WHEN** the returned `WorkerPrompt` is examined
- **THEN** `WorkerContext.omissions` includes an entry for the missing shared core goal

---

### Requirement: Upstream forwarding SHALL respect the ContextPlan token budget, dropping retrieval first

When upstream units plus the shared core plus native retrieval units exceed the remaining token budget of the ContextPlan, the ContextPackCompiler SHALL exclude native retrieval units before upstream units, and SHALL exclude upstream units only when no native retrieval unit remains. The shared core (`required:shared-core` key) SHALL never be excluded. All exclusions SHALL be recorded in `WorkerContext.omissions`.

#### Scenario: Budget overflow drops a native unit before an upstream unit

- **GIVEN** a ContextPlan with a token budget that fits the shared core and one upstream unit but not an additional native unit
- **WHEN** the pack is compiled
- **THEN** the upstream unit is included
- **AND** the native unit is excluded
- **AND** an omission is recorded for the native unit

#### Scenario: Shared core survives an overflow that drops every other unit

- **GIVEN** a ContextPlan with a token budget that fits only the shared core
- **WHEN** the pack is compiled
- **THEN** the shared core unit is included
- **AND** all native and upstream units are excluded with omission notes

---

### Requirement: `verdict bench context-lift --forwarding` SHALL test the forwarding mechanism with a control arm

The `--forwarding` mode SHALL run a two-node synthetic shape at two pack sizes. It SHALL
refuse to run without a committed stop-rule file whose hash it embeds in the evidence JSON.
Routes SHALL be drawn from `_FREE_COST_CLASSES` only. The evidence JSON SHALL include
`"non_python_code_facts_absent": true`. The bench SHALL exit 0 on blocked routes.


#### Scenario: Bench produces forwarding evidence on success

- **GIVEN** at least one free route is live
- **AND** a stop-rule file is present
- **WHEN** `verdict bench context-lift --forwarding` is run
- **THEN** `$VERDICT_HOME/context-lift/latest-forwarding.json` is written
- **AND** the evidence JSON contains `arm_b_pass_rate`, `arm_c_pass_rate`, and results at both pack sizes
- **AND** `non_python_code_facts_absent` is `true` in the evidence JSON

#### Scenario: Bench refuses without stop-rule file

- **GIVEN** no stop-rule file is present
- **WHEN** `verdict bench context-lift --forwarding` is run
- **THEN** the bench exits with a non-zero exit code and an error message citing the missing stop rule

#### Scenario: Bench exits 0 on blocked routes

- **GIVEN** no free routes are live (all blocked)
- **WHEN** `verdict bench context-lift --forwarding` is run
- **THEN** the bench exits 0
- **AND** the evidence JSON contains `"conclusion": "blocked"`
