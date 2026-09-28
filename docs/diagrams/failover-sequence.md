# Failover Sequence — Single Node

What happens when a worker node fails during orchestration execution.
The entire recovery loop is inline in `DagRuntime._drive()`
(`verdict/orchestration/runtime.py`); there is no separate failover engine
or bounded-recovery controller.

```mermaid
sequenceDiagram
    participant RT as DagRuntime._drive()<br/>(verdict/orchestration/runtime.py)
    participant Sel as ModelSelector.select()<br/>(verdict/orchestration/contracts.py)
    participant Exec as PrimeHeadlessExecutor<br/>(verdict/orchestration/executors.py)
    participant FI as FailureIntelligence.classify()<br/>(verdict/orchestration/recovery.py)

    Note over RT: tried = ∅

    RT->>Sel: select(requirements, exclude_routes=tried)
    Sel->>Sel: eligibility ladder<br/>DISCOVERED → ENTITLED → HEALTHY →<br/>AVAILABLE → TASK_ELIGIBLE → SELECTED
    Sel-->>RT: choice (model A, route_id)

    RT->>RT: require_launchable(route_id)<br/>dispatch_blocker(route_id)
    RT->>Exec: dispatch worker (model A, node N)

    Note over Exec: Worker hits provider error<br/>(429 / 502 / timeout)

    Exec-->>RT: WorkerTerminal (FAILURE)

    RT->>FI: classify(terminal)
    FI-->>RT: FailureClassification<br/>(category, cooldown_seconds, scope)

    RT->>Sel: record_failure(route_id, classification)<br/>cooldown with scope (route | provider)
    Note over RT: tried.add(route_id)<br/>sleep(min(cooldown, 60s))

    RT->>Sel: select(requirements, exclude_routes=tried)

    alt No eligible model & short cooldown pending
        Note over RT: Wait for earliest cooldown<br/>(≤ max_cooldown_wait_seconds)
        RT->>Sel: select(requirements, exclude_routes=tried)
    end

    alt Pool exhausted (no eligible model)
        Note over RT: pool_exhausted → FAIL_CLOSED<br/>Node → BLOCKED
    else Model B selected
        Sel-->>RT: choice (model B, route_id)
        RT->>RT: require_launchable(route_id)<br/>dispatch_blocker(route_id)

        alt Pre-dispatch blocker found
            Note over RT: tried.add(route_id)<br/>continue loop (reselect)
        else Clear to launch
            RT->>Exec: redispatch worker (model B, node N)
            Exec-->>RT: WorkerTerminal (SUCCESS)
            Note over RT: Node → TERMINAL_SUCCESS → review
        end
    end
```

**Note:** `ControllerSupervisor` (`verdict/orchestration/supervisor.py`) wraps
the *controller process* via `orchestration/cli.py`, not individual worker
nodes inside the DAG runtime.
