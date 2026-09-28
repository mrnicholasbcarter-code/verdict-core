# Goal-to-Receipt Pipeline

How a goal becomes a verified receipt through the orchestration pipeline.

```mermaid
flowchart TD
    subgraph Input
        GOAL["Goal / OpenSpec change\n(verdict/openspec_lifecycle.py)"]
    end

    subgraph Planning ["Frontier Planning"]
        PLANNER["Frontier Planner\n(verdict/orchestration/planner.py)"]
        WORKGRAPH["WorkGraph — DAG of WorkNodes\n(verdict/orchestration/contracts.py)"]
    end

    subgraph Admission ["Hard Admission & Eligibility"]
        ADMIT["Admitted Set — admit()\n(verdict/admission.py)"]
        ELIGIBILITY["Eligibility Ladder\n(verdict/orchestration/eligibility.py)"]
        STAGE_D["DISCOVERED — in OmniRoute inventory"]
        STAGE_E["ENTITLED — active provider account"]
        STAGE_H["HEALTHY — 1-token probe passed\n(verdict/probes.py)"]
        STAGE_A["AVAILABLE — no cooldown/quota block"]
        STAGE_T["TASK_ELIGIBLE — capability & policy fit"]
        STAGE_S["SELECTED"]
    end

    subgraph Context ["Context Engine"]
        HYDRATE["Context Hydrate — gather sources\n(verdict/context_hydrate.py)"]
        PACK["Context Pack — compile envelope\n(verdict/context_pack.py)"]
    end

    subgraph Dispatch ["Role Assignment & Workers"]
        DISPATCH["Dispatcher — role assignment\n(verdict/dispatcher.py)"]
        EXECUTORS["PrimeHeadlessExecutor\n(verdict/orchestration/executors.py)"]
        SUPERVISOR["Supervisor — health monitor\n(verdict/orchestration/supervisor.py)"]
        RUN["Run loop — parallel workers\n(verdict/orchestration/run.py)"]
    end

    subgraph Recovery ["Failure-Directed Recovery"]
        RECOVERY["Recovery — classify & reroute\n(verdict/orchestration/recovery.py)"]
        BOUNDED["Bounded Recovery — action policy\n(verdict/bounded_recovery.py)"]
        FAILOVER["Failover Engine — replacement\n(verdict/failover_engine.py)"]
    end

    subgraph Verification ["Review & Receipt"]
        REVIEW["Independent OCR reviewer\n(verdict/orchestration/review.py)"]
        RECEIPT["Receipt — SHA-256 digest\n(verdict/orchestration/receipt.py)"]
        OUTCOME["Outcome Records\n(verdict/outcome_records.py)"]
    end

    GOAL --> PLANNER
    PLANNER --> WORKGRAPH
    WORKGRAPH --> ADMIT
    ADMIT --> ELIGIBILITY
    ELIGIBILITY --> STAGE_D --> STAGE_E --> STAGE_H --> STAGE_A --> STAGE_T --> STAGE_S
    STAGE_S --> HYDRATE
    HYDRATE --> PACK
    PACK --> DISPATCH
    DISPATCH --> EXECUTORS
    EXECUTORS --> RUN
    SUPERVISOR -.->|monitors| RUN
    RUN -->|success| REVIEW
    RUN -->|failure| RECOVERY
    RECOVERY --> BOUNDED
    BOUNDED -->|rehydrate/retry| HYDRATE
    BOUNDED -->|escalate/replan| PLANNER
    FAILOVER -.->|replacement model| RECOVERY
    REVIEW -->|accepted| RECEIPT
    REVIEW -->|rejected| RECOVERY
    RECEIPT --> OUTCOME
```
