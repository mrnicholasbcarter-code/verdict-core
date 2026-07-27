# @claude-flow/guidance Integration in Verdict Core

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        VERDICT CORE EXECUTION PIPELINE                          │
└─────────────────────────────────────────────────────────────────────────────────┘

    ┌──────────────┐
    │  CLAUDE CODE │  ◄─── User / Agent requests execution
    └──────┬───────┘
           │
           ▼
    ┌──────────────────────┐
    │  TASKSPEC NORMALIZE  │  ◄─── Every request → TaskSpec (single source of truth)
    │  ┌────────────────┐  │       Goal, Files, Risk, Protected, Capabilities,
    │  │ Raw Input:     │  │       Privacy, Auth, Quality, Verification,
    │  │ string | dict  │  │       Rollback, Tool Intent, Metadata
    │  └────────────────┘  │
    └──────────┬───────────┘
               │
               ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │              GUIDANCE CONTROL PLANE (SINGLETON)                 │
    │  ┌─────────────────────────────────────────────────────────┐   │
    │  │  POLICY BUNDLE (compiled once at startup)                │   │
    │  │  • CLAUDE.md → Constitution (rare changes)              │   │
    │  │  • CLAUDE.local.md → Local Overlay (frequent changes)   │   │
    │  │  • Versioned, cached, hash-verified                     │   │
    │  └─────────────────────────────────────────────────────────┘   │
    └──────────────────────────┬────────────────────────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
    ┌──────────────────┐ ┌─────────────┐ ┌──────────────┐
    │ GUIDANCE RETRIEVAL│ │  GUIDANCE   │ │  VERDICT     │
    │ (Intent-based)   │ │   GATES     │ │  INTEGRATION │
    │                  │ │             │ │              │
    │ • Constitution   │ │ • Eligibility│ │ • Eligibility│
    │ • Local Overlay  │ │   Gate      │ │   Gate       │
    │ • Task-Specific  │ │ • Privacy   │ │ • Privacy    │
    │   Rules          │ │   Gate      │ │   Gate       │
    │ • Top 20 shards  │ │ • Budget    │ │ • Budget     │
    └────────┬─────────┘ └──────┬──────┘ └──────┬───────┘
             │                  │               │
             │                  ▼               │
             │         ┌──────────────┐         │
             │         │   DENY       │─────────┘
             │         │  STOPS ALL   │
             │         └──────────────┘
             │
             ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    TOOL GATEWAY                                 │
    │  Every tool call MUST pass through:                            │
    │  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ │
    │  │ BUDGETS    │ │IDEMPOTENCY │ │  SCHEMAS   │ │PROTECTED CMD│ │
    │  │ Token/Time │ │  (writes)  │ │  Validation│ │  Blocked    │ │
    │  │ Cost/Call  │ │  Prevents  │ │  Required  │ │  Git push   │ │
    │  │ Limits     │ │  Duplicates│ │  Forbidden │ │  --force,   │ │
    │  └────────────┘ └────────────┘ └────────────┘ └────────────┘ │
    └────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    CONTINUE GATE                                │
    │  Evaluated after EVERY autonomous step:                        │
    │  ┌─────────┐ ┌────────────┐ ┌─────────┐ ┌──────┐ ┌──────┐    │
    │  │CONTINUE │ │CHECKPOINT  │ │THROTTLE │ │PAUSE │ │ STOP │    │
    │  │(normal) │ │(interval)  │ │(limits) │ │(wait)│ │(fail)│    │
    │  └─────────┘ └────────────┘ └─────────┘ └──────┘ └──────┘    │
    │  • Loop detection (repeated approaches)                        │
    │  • Consecutive failure tracking                                │
    │  • Token/Cost/Duration budgets                                 │
    │  • Integrates with Ruflo Completion Autopilot                  │
    └────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    MEMORY GATE                                  │
    │  Every memory write requires:                                  │
    │  ┌────────────┐ ┌────────┐ ┌──────────────┐ ┌────────────┐   │
    │  │ AUTHORITY  │ │  TTL   │ │ CONTRADICTION│ │ CONFIDENCE │   │
    │  │ (system/   │ │ Max/   │ │  DETECTION   │ │ Threshold  │   │
    │  │ verdict/   │ │ Default│ │  (semantic)  │ │ (0.5-1.0)  │   │
    │  │ agent)     │ │  per ns│ │              │ │            │   │
    │  └────────────┘ └────────┘ └──────────────┘ └────────────┘   │
    │  ┌────────────┐                                                │
    │  │ PROVENANCE │  Required for all writes                      │
    │  │ (source,   │                                                │
    │  │  timestamp,│                                                │
    │  │  task_id)  │                                                │
    │  └────────────┘                                                │
    └────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    RUFLO ORCHESTRATION                          │
    │  ┌──────────────┐ ┌──────────────┐ ┌────────────────────────┐  │
    │  │ HIERARCHICAL │ │  SPECIALIZED │ │  SWARM DISPATCHER      │  │
    │  │  TOPOLOGY    │ │  STRATEGY    │ │  • researcher → architect│  │
    │  │  (anti-drift)│ │  (roles)     │ │    → coder → tester     │  │
    │  │  maxAgents=8 │ │              │ │    → reviewer           │  │
    │  └──────────────┘ └──────────────┘ └────────────────────────┘  │
    └────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    OMNIROUTE / PROVIDER                         │
    │  ┌────────────────────────────────────────────────────────────┐│
    │  │ Model Selection: Haiku | Sonnet | Opus | Frontier         ││
    │  │ Fallback Chain: Primary → Secondary → Tertiary             ││
    │  │ Cost-Optimal KRR Routing with Neural Augmentation          ││
    │  └────────────────────────────────────────────────────────────┘│
    └────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    EXECUTION & VERIFICATION                     │
    │  • Actual provider/model execution                             │
    │  • Quality threshold verification                              │
    │  • Budget compliance check                                     │
    │  • Test execution if applicable                                │
    └────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    PROOF CHAIN (AUDIT TRAIL)                    │
    │  Each entry cryptographically chained:                         │
    │  ┌──────────────────────────────────────────────────────────┐  │
    │  │ index | timestamp | task_hash | shard_hashes | decisions │  │
    │  │ verdict_result | tool_calls | omniroute | provider | model│  │
    │  │ fallback_history | previous_hash | current_hash          │  │
    │  └──────────────────────────────────────────────────────────┘  │
    │  • Immutable, append-only                                     │
    │  • Exportable for compliance/audit                            │
    └────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                    LEARNING / OPTIMIZATION                      │
    │  • Store successful patterns in Ruflo memory                  │
    │  • Record failed approaches for avoidance                     │
    │  • Update SONA/MoE neural patterns                            │
    │  • Train ReasoningBank with EWC++ consolidation               │
    └────────────────────────────────────────────────────────────────┘
```

## Sequence Diagram

```
┌─────────┐     ┌─────────────────┐     ┌────────────────────┐     ┌────────────────┐
│  User   │     │  CLAUDE CODE    │     │  GUIDANCE CONTROL  │     │   VERDICT      │
│  /Agent │     │                 │     │      PLANE         │     │     CORE       │
└────┬────┘     └────────┬────────┘     └─────────┬──────────┘     └───────┬────────┘
     │                   │                        │                    │
     │ 1. Request        │                        │                    │
     │ ────────────────► │                        │                    │
     │                   │ 2. normalize_task_spec()                   │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │ 3. TaskSpec      │
     │                   │                        │ ◄───────────────  │
     │                   │                        │                    │
     │                   │ 4. retrieve_guidance(TaskSpec)             │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │ 5. Shards[]      │
     │                   │                        │ ◄───────────────  │
     │                   │                        │                    │
     │                   │ 6. evaluate_verdict_gates()                │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │ 7. GateDecisions │
     │                   │                        │ ◄───────────────  │
     │                   │                        │                    │
     │                   │ 7a. IF DENY: Stop & Return Error           │
     │                   │                        │                    │
     │                   │ 8. start_proof_chain_entry()               │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │                    │
     │                   │ 9. Tool Gateway checks                       │
     │                   │ ◄────────────────────────────────────────  │
     │                   │                        │                    │
     │                   │ 10. Continue Gate eval (per step)          │
     │                   │ ◄────────────────────────────────────────  │
     │                   │                        │                    │
     │                   │ 11. Memory Gate (if writes)                │
     │                   │ ◄────────────────────────────────────────  │
     │                   │                        │                    │
     │                   │ 12. Dispatch to Ruflo                      │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │                    │
     │                   │ 13. Route via OmniRoute                    │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │ 14. Routing      │
     │                   │                        │ ◄───────────────  │
     │                   │                        │                    │
     │                   │ 15. Execute with Provider                  │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │ 16. Result       │
     │                   │                        │ ◄───────────────  │
     │                   │                        │                    │
     │                   │ 17. Verify Execution                     │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │ 18. Verification │
     │                   │                        │ ◄───────────────  │
     │                   │                        │                    │
     │                   │ 19. Record in Proof Chain                │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │                    │
     │                   │ 20. Learning / Pattern Store             │
     │                   │ ──────────────────────────────────────►    │
     │                   │                        │                    │
     │                   │ 21. Return Result                        │
     │                   │ ◄────────────────────────────────────────  │
     │ 22. Result ◄───── │                        │                    │
     │                   │                        │                    │
```

## Key Integration Points

### 1. **Singleton Initialization** (Phase 2)
- `GuidanceControlPlane.initialize()` called once in FastAPI lifespan
- Loads CLAUDE.md + CLAUDE.local.md into compiled policy bundle
- Creates eligibility gate with availability cache

### 2. **TaskSpec Normalization** (Phase 3)
- `normalize_task_spec()` converts any input to structured TaskSpec
- Single source of truth for all downstream decisions
- Deterministic task_id generation

### 3. **Guidance Retrieval** (Phase 4)
- `retrieve_guidance()` uses intent classification (keyword scoring)
- Returns top 20 relevant shards, not all rules
- Constitution + Local overlay + Task-specific rules

### 4. **Verdict Gates** (Phase 5)
- `evaluate_verdict_gates()` runs eligibility, privacy, budget gates
- DENY stops execution immediately
- FAIL_VISIBLE allows read-only investigation

### 5. **Tool Gateway** (Phase 6)
- `ToolGateway.check_tool_call()` enforces:
  - Budget limits (tokens, time, cost, call count)
  - Idempotency (prevents duplicate writes)
  - Schema validation (required/forbidden args)
  - Protected commands (git --force, rm -rf, db drops)

### 6. **Git Protection** (Phase 7)
- `check_git_operation()` blocks dangerous git commands
- Runtime gates instead of prompt text
- Covers: push --force, reset --hard, clean -fd, checkout --, restore

### 7. **Continue Gate** (Phase 8)
- `ContinueGate.evaluate()` after every step
- States: CONTINUE, CHECKPOINT, THROTTLE, PAUSE, STOP
- Loop detection via approach repetition tracking
- Integrates with Ruflo Autopilot

### 8. **Memory Gate** (Phase 9)
- `MemoryGate.evaluate_write()` requires:
  - Authority verification (system > verdict > ruflo > agent)
  - TTL limits per namespace
  - Contradiction detection
  - Confidence thresholds
  - Provenance metadata

### 9. **Proof Chain** (Phase 10)
- `ProofChainEntry` with cryptographic hash chain
- Records: task spec, shards, gate decisions, verdict, tools, routing, provider, fallback
- Exportable for audit/compliance

### 10. **Pipeline** (Phase 11)
- `execute_pipeline()` orchestrates full flow
- Order: Normalize → Retrieve → Gates → Verdict → Tools → Ruflo → OmniRoute → Execute → Verify → Proof → Learn

### 11. **API Endpoints** (Integration)
- `GET /v1/guidance/status` - Control plane status
- `POST /v1/guidance/execute` - Full pipeline execution
- `GET /v1/guidance/proof-chain` - Audit trail
- `GET /v1/guidance/policy` - Current policy bundle

## Success Criteria Verification

✅ **Every command passes through:**
1. TaskSpec normalization
2. Guidance Retrieval
3. Guidance Gates
4. Verdict Core
5. Tool Gateway
6. Execution

✅ **If any layer denies → execution stops** (DENY, DEFER, FAIL_VISIBLE)

✅ **Runtime enforcement, not prompt instructions** - all gates are code, not text

✅ **Evidence of enforcement:**
- Protected git commands blocked by `ToolGateway._check_protected_commands()`
- Memory writes require authority/TTL/confidence via `MemoryGate`
- Loop detection via `ContinueGate._detect_loop()`
- Proof chain records every decision with cryptographic hashes
- Budget limits enforced in both Tool Gateway and Continue Gate

## Files Created

| File | Purpose |
|------|---------|
| `verdict/guidance_control_plane.py` | Main singleton control plane |
| `verdict/tool_gateway.py` | Tool call enforcement gateway |
| `verdict/continue_gate.py` | Autonomous step evaluation |
| `verdict/memory_gate.py` | Memory write governance |
| `verdict/api.py` | FastAPI endpoints for guidance |
| `tests/test_guidance_control_plane.py` | Comprehensive tests |