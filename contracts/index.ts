/** Version 1 shared contracts. This module has no runtime dependency on a planner. */
export type JsonObject = { [key: string]: unknown };
export type Signal = { source: string; freshness_seconds?: number; [key: string]: unknown };
export interface CapabilityRequirement { capability: string; required?: boolean; minimum_level?: string | null; reason?: string | null; schema_version?: "1" }
export interface TaskSpec extends JsonObject { objective: string; task_type: string; criticality?: string; privacy?: string; risk?: string; budget?: JsonObject; latency?: JsonObject | null; verification?: JsonObject | null; schema_version?: "1" }
export interface RuntimeCandidate extends JsonObject { runtime_id: string; catalog_present: boolean; live_eligible: boolean; availability: string; signals: Record<string, Signal>; capabilities?: string[]; provider?: string | null; model?: string | null; schema_version?: "1" }
export interface AvailabilitySnapshot extends JsonObject { observed_at: string; candidates?: RuntimeCandidate[]; state?: string; signals?: Record<string, Signal>; schema_version?: "1" }
export interface WorkflowPlan extends JsonObject { steps: JsonObject[]; plan_id?: string | null; fallback_allowed?: boolean; schema_version?: "1" }
export interface RoutingDecision extends JsonObject { selected_route: JsonObject; exclusions?: JsonObject[]; policy_floor?: string; candidate_snapshot?: string | JsonObject | null; adaptive_influence?: JsonObject; fallback_plan?: JsonObject[]; correlation_id?: string | null; schema_version?: "1" }
export interface FallbackAttempt extends JsonObject { runtime_id: string; reason: string; legal: boolean; schema_version?: "1" }
export interface VerificationPlan extends JsonObject { checks: unknown[]; on_failure?: string; schema_version?: "1" }
export interface OutcomeEvent extends JsonObject { event_id?: string | null; event_type?: string | null; correlation_id?: string | null; outcome?: string | null; occurred_at?: string | null; request_id?: string | null; schema_version?: "1" }

const secret = /(^|_)(api_key|apikey|authorization|password|secret|token)$/i;
const contractFields: Record<string, Set<string>> = {
  TaskSpec: new Set(["objective", "task_type", "effort", "reasoning", "capabilities", "required_capabilities", "tools", "context", "context_requirements", "tool_requirements", "privacy", "risk", "budget", "latency", "latency_limit_ms", "workflow", "approvals", "criticality", "verification", "parallelism", "destructive_operation", "production_impact", "degraded_mode_policy", "metadata", "schema_version"]),
  RuntimeCandidate: new Set(["runtime_id", "catalog_present", "live_eligible", "availability", "signals", "capabilities", "provider", "model", "model_version", "schema_version"]),
};
function rejectSecrets(value: unknown): void { if (Array.isArray(value)) value.forEach(rejectSecrets); else if (value && typeof value === "object") for (const [key, child] of Object.entries(value)) { if (secret.test(key)) throw new Error(`secret-bearing field rejected: ${key}`); rejectSecrets(child); } }
export function parseContract<T extends JsonObject>(name: string, value: T): T { rejectSecrets(value); const fields = contractFields[name]; if (fields) for (const key of Object.keys(value)) if (!fields.has(key)) throw new Error(`unknown field: ${key}`); return JSON.parse(JSON.stringify(value)) as T; }
export function serializeContract(value: JsonObject): string { rejectSecrets(value); return JSON.stringify(value); }
