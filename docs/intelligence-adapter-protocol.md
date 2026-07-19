# Intelligence Adapter Protocol v1

**Version:** `intelligence-adapter/v1`  
**Status:** Verification & Routing Specification

This protocol defines the interfaces and invariants for all communication between the public `llm-gate` gateway and the underlying Ruflo/RuVector managed intelligence engines.

---

## 1. Compliance & Security Boundaries

1. **Protocol Isolation**: `llm-gate` interacts with Ruflo/RuVector exclusively through versioned subprocess CLI interfaces. The gateway **MUST NOT** directly access SQLite databases (`memory.db`/`state.db`) or raw database tables of the underlying runtimes.
2. **Subprocess Control**: Subprocess calls MUST use strictly structured execution (`subprocess.run(["ruflo", ...], shell=False)` or async equivalent) to eliminate shell command injection risks. Async contexts of FastAPI must invoke these non-blockingly.
3. **Data Redaction**: Telemetry, prompt hashes, and outcome events MUST be redacted. Raw prompts, credential files, or unmasked completing bytes must not be sent to the managed interfaces.

---

## 2. Configuration Settings

The following environment variables are queried at runtime and validated upon initialization:

```bash
LLMGATE_INTELLIGENCE_MODE=production|development_degraded
LLMGATE_INTELLIGENCE_TIMEOUT_MS=250          # Positive integer limit
LLMGATE_INTELLIGENCE_ADAPTER=managed|local
LLMGATE_RUFLO_COMMAND=ruflo                 # Executable path/name
LLMGATE_RUVECTOR_COMMAND=ruvector            # Executable path/name
LLMGATE_POLICY_VERSION=policy-YYYY-MM-DD.N
LLMGATE_PRIVACY_MODE=redacted|local_only
LLMGATE_LEARNING_ENABLED=true|false
```

---

## 3. Subprocess CLI Commands

### 1. Security Compliance & Content Checks
Checks command/content payloads.
```bash
ruflo guidance gates --command <command> --content <content> --json
```

### 2. Model Routing Suggestion
Asks Ruflo for model ranking recommendations based on task class.
```bash
ruflo hooks model-route -t <task_class> --context <context_len>
```
*Response Envelope:*
```json
{
  "protocol": "intelligence-adapter/v1",
  "operation": "route",
  "request_id": "uuid",
  "status": "ok",
  "ranking": [
    {"model_id": "kc/tencent/hy3:free", "adjustment": 0.02, "confidence": 0.61}
  ]
}
```

### 3. Trajectory Memory & Learning Outcomes
Allows recording task workflows and trajectories for the SONA neural loop.
```bash
ruvector hooks trajectory-begin --context <ctx> --agent <agent_name>
ruvector hooks trajectory-step --action <action> --result <result> --reward <reward_float>
ruvector hooks trajectory-end --success <boolean> --quality <quality_float>
```

At the completion of a query, outcome telemetry is reported via:
```bash
ruflo hooks model-outcome -t <task_class> -m <model_id> -o <transport_outcome> -q <quality_outcome>
```

---

## 4. Readiness & Health Enforcement

* In **Production Mode**, `/ready` reports healthy checks (`ready`) only if the managed Ruflo/RuVector subprocess outputs a valid readiness state. If the adapter is unresponsive, the service fails closed.
* In **Development Degraded Mode**, the service reports `degraded` but remains functional by falling back to the local deterministic safety floor settings.
