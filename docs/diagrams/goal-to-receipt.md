# Goal-to-Receipt Pipeline

How a goal becomes a verified receipt through the orchestration pipeline.

Source: [`diagrams/goal-to-receipt.mmd`](../../diagrams/goal-to-receipt.mmd).
The fenced block below is byte-identical to that file.

```mermaid
%% goal-to-receipt.mmd -- goal to verified receipt
%% Source (verified against code at this commit):
%%   verdict/orchestration/planner.py (FrontierPlanner)
%%   verdict/orchestration/contracts.py (WorkGraph, ModelSelector.select)
%%   verdict/orchestration/run.py (run loop)
%%   verdict/orchestration/runtime.py (DagRuntime._drive)
%%   verdict/orchestration/eligibility.py (EligibilityLadder)
%%   verdict/probes.py (ProbeRunner)
%%   verdict/orchestration/executors.py (PrimeHeadlessExecutor)
%%   verdict/orchestration/recovery.py (FailureIntelligence.classify)
%%   verdict/orchestration/review.py (OpenCodeReviewer.review)
%%   verdict/orchestration/receipt.py (build_run_receipt)
%%   verdict/outcome_records.py (outcome records)
%%   verdict/orchestration/supervisor.py (ControllerSupervisor)
%%   verdict/openspec_lifecycle.py (OpenSpec change intake)
%%   NOTE: a rejected independent review ends the run BLOCKED via DagRuntime._finish
%%   (runtime.py:1306-1311); it does not re-enter FailureIntelligence.classify.
%% Verdict visual theme. Hex from verdict/design.py PALETTE:
%% background #101014  surface #18181b  text #f4f4f5  secondary #a1a1aa  muted #92929e
%% purple #a78bfa  cyan #22b8eb  success #4ade80  amber #f5b00b  red #f87171  border #52525b
%% Edges/lines use #7c7c88 (>=3:1 on #ffffff and #0d1117; measured 4.12:1 / 4.59:1).
%% fontFamily omitted: Mermaid sanitizeDirective drops hyphenated themeVariable values.
%%{init: {'theme':'base','themeVariables':{'darkMode':true,'background':'#101014','fontSize':'15px','primaryColor':'#18181b','primaryTextColor':'#f4f4f5','primaryBorderColor':'#52525b','secondaryColor':'#18181b','secondaryTextColor':'#f4f4f5','secondaryBorderColor':'#52525b','tertiaryColor':'#101014','tertiaryTextColor':'#f4f4f5','tertiaryBorderColor':'#52525b','lineColor':'#7c7c88','textColor':'#f4f4f5','mainBkg':'#18181b','nodeTextColor':'#f4f4f5','nodeBorder':'#52525b','clusterBkg':'#101014','clusterBorder':'#52525b','titleColor':'#f4f4f5','edgeLabelBackground':'#18181b','noteBkgColor':'#18181b','noteTextColor':'#f4f4f5','noteBorderColor':'#52525b','actorBkg':'#18181b','actorBorder':'#a78bfa','actorTextColor':'#f4f4f5','actorLineColor':'#7c7c88','signalColor':'#7c7c88','signalTextColor':'#f4f4f5','labelBoxBkgColor':'#18181b','labelTextColor':'#f4f4f5','loopTextColor':'#f4f4f5','activationBkgColor':'#101014','activationBorderColor':'#22b8eb','sequenceNumberColor':'#101014'}}}%%
flowchart TD
    subgraph Input
        GOAL["Goal or OpenSpec change (verdict/openspec_lifecycle.py)"]
    end

    subgraph Planning ["Frontier planning"]
        PLANNER["FrontierPlanner.plan (verdict/orchestration/planner.py)"]
        WORKGRAPH["WorkGraph, a DAG of work nodes (verdict/orchestration/contracts.py)"]
    end

    subgraph Execution ["Runtime and workers"]
        RUN["Run loop, parallel workers (verdict/orchestration/run.py)"]
        RUNTIME["DagRuntime._drive, per-node loop (verdict/orchestration/runtime.py)"]
        SELECTOR["ModelSelector.select (verdict/orchestration/contracts.py)"]
        EXECUTORS["PrimeHeadlessExecutor (verdict/orchestration/executors.py)"]
    end

    subgraph Eligibility ["Eligibility ladder"]
        ELIG["EligibilityLadder (verdict/orchestration/eligibility.py)"]
        STAGE_D["DISCOVERED, in the OmniRoute inventory"]
        STAGE_E["ENTITLED, active provider account"]
        STAGE_H["HEALTHY, 1-token probe passed (verdict/probes.py)"]
        STAGE_A["AVAILABLE, no cooldown or quota block"]
        STAGE_T["TASK_ELIGIBLE, capability and policy fit"]
        STAGE_S["SELECTED"]
    end

    subgraph Recovery ["Inline recovery inside _drive"]
        CLASSIFY["FailureIntelligence.classify (verdict/orchestration/recovery.py)"]
        COOLDOWN["Record cooldown, scope route or provider"]
        RESELECT["select again, exclude_routes=tried"]
        FAILCLOSED["Pool exhausted, FAIL_CLOSED"]
    end

    subgraph Verification ["Review and receipt"]
        REVIEW["Independent OCR reviewer (verdict/orchestration/review.py)"]
        RBLOCK["Run BLOCKED via _finish (runtime.py:1306-1311)"]
        RECEIPT["Receipt, SHA-256 event digest (verdict/orchestration/receipt.py)"]
        OUTCOME["Outcome records (verdict/outcome_records.py)"]
    end

    subgraph Controller ["Controller supervision"]
        SUP["ControllerSupervisor wraps the controller process (verdict/orchestration/supervisor.py)"]
    end

    GOAL --> PLANNER
    PLANNER --> WORKGRAPH
    WORKGRAPH --> RUN
    RUN --> RUNTIME
    RUNTIME --> SELECTOR
    SELECTOR --> ELIG
    ELIG --> STAGE_D --> STAGE_E --> STAGE_H --> STAGE_A --> STAGE_T --> STAGE_S
    STAGE_S --> EXECUTORS
    RUN -->|success| REVIEW
    RUN -->|failure| CLASSIFY
    CLASSIFY --> COOLDOWN
    COOLDOWN --> RESELECT
    RESELECT -->|model available| EXECUTORS
    RESELECT -->|pool exhausted| FAILCLOSED
    REVIEW -->|accepted| RECEIPT
    REVIEW -->|rejected| RBLOCK
    RECEIPT --> OUTCOME
    SUP -.->|wraps| RUN

classDef active fill:#18181b,stroke:#22b8eb,color:#f4f4f5,stroke-width:2px
classDef selected fill:#18181b,stroke:#a78bfa,color:#f4f4f5,stroke-width:2px
classDef cooldown fill:#18181b,stroke:#f5b00b,color:#f4f4f5,stroke-width:2px
classDef failed fill:#18181b,stroke:#f87171,color:#f4f4f5,stroke-width:2px
classDef validated fill:#18181b,stroke:#4ade80,color:#f4f4f5,stroke-width:2px
classDef muted fill:#18181b,stroke:#52525b,color:#a1a1aa,stroke-width:1px
class GOAL,PLANNER,RUN,RUNTIME,SELECTOR,ELIG active
class STAGE_S,EXECUTORS,REVIEW,RECEIPT,OUTCOME selected
class COOLDOWN,RESELECT cooldown
class FAILCLOSED,RBLOCK failed
class STAGE_D,STAGE_E,STAGE_H,STAGE_A,STAGE_T,WORKGRAPH,CLASSIFY validated
class SUP muted
```
