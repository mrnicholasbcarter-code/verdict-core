/**
 * Version 1 shared contracts.
 *
 * This module intentionally contains no planner, router, or provider logic.
 * Zod is used only at the contract boundary so TypeScript cannot accept a
 * payload that the Python runtime would reject.
 */
import { z, ZodError, type ZodType } from "zod";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: unknown };
export type Signal = { source: string; freshness_seconds?: number; [key: string]: unknown };

export type ContractErrorCategory =
  | "invalid_type"
  | "missing_field"
  | "unknown_field"
  | "invalid_value"
  | "schema_version"
  | "secret_bearing"
  | "unsafe_workflow";

export class ContractValidationError extends Error {
  readonly category: ContractErrorCategory;
  readonly path: readonly (string | number)[];

  constructor(
    category: ContractErrorCategory,
    message: string,
    path: readonly (string | number)[] = [],
  ) {
    super(message);
    this.name = "ContractValidationError";
    this.category = category;
    this.path = path;
  }
}

const secretKey = /^(api_key|apikey|authorization|password|secret|token)$/i;
const secretSuffix = /(?:_token|_secret|_password|_api_key)$/i;
const sensitiveContentKey = /^(prompt|raw_prompt|completion|raw_completion|messages)$/i;
const secretTextPatterns = [
  /(?<prefix>authorization\s*:\s*bearer\s+)[^\s,;]+/giu,
  /(?<name>api[_-]?key|token|password|secret)(?<separator>\s*[=:]\s*)[^\s&,;]+/giu,
];

function normalizedKey(key: string): string {
  return key.toLowerCase().replaceAll("-", "_");
}

function isSecretKey(key: string): boolean {
  const normalized = normalizedKey(key);
  return secretKey.test(normalized) || secretSuffix.test(normalized);
}

function isSensitiveContentKey(key: string): boolean {
  return sensitiveContentKey.test(normalizedKey(key));
}

/** Redact secret-like keys and raw prompt/completion fields recursively. */
export function redactContractSecrets(value: unknown): unknown {
  if (typeof value === "string") {
    let redacted = value.replace(/(https?:\/\/)([^/@:]+)(?::[^/@]*)?@/giu, "$1[redacted]@");
    for (const pattern of secretTextPatterns) {
      redacted = redacted.replace(pattern, (_match, ...captures: unknown[]) => {
        const groups = captures.at(-1) as Record<string, string> | undefined;
        if (groups?.prefix) return `${groups.prefix}[redacted]`;
        if (groups?.name && groups.separator) return `${groups.name}${groups.separator}[redacted]`;
        return "[redacted]";
      });
    }
    return redacted;
  }
  if (Array.isArray(value)) return value.map(redactContractSecrets);
  if (value && typeof value === "object") {
    const redacted: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value)) {
      redacted[key] = isSecretKey(key) || isSensitiveContentKey(key)
        ? "[redacted]"
        : redactContractSecrets(child);
    }
    return redacted;
  }
  return value;
}

function isRedacted(value: unknown): boolean {
  return value === "[redacted]";
}

function rejectSecrets(value: unknown, path: readonly (string | number)[] = []): void {
  if (Array.isArray(value)) {
    value.forEach((child, index) => rejectSecrets(child, [...path, index]));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value)) {
    if (isSecretKey(key) && !isRedacted(child)) {
      throw new ContractValidationError(
        "secret_bearing",
        `secret-bearing field rejected: ${key}`,
        [...path, key],
      );
    }
    rejectSecrets(child, [...path, key]);
  }
}

const jsonObject = z.record(z.string(), z.unknown());
const nullableString = z.string().nullable();
const schemaVersion = z.literal("1");
const nonEmptyString = z.string().trim().min(1);
const nonNegativeNumber = z.number().finite().nonnegative();
const nonNegativeInteger = z.number().int().nonnegative();

const workflowActions = [
  "answer",
  "research",
  "implement",
  "review",
  "verify",
  "synthesis",
  "specialist",
  "human_approval",
  "execute",
] as const;
const availabilityStates = [
  "eligible",
  "ready",
  "healthy",
  "outage",
  "degraded",
  "unknown",
  "unavailable",
  "denied",
  "quota_exhausted",
  "rate_limited",
  "unauthorized",
  "locked_out",
  "circuit_open",
  "timeout",
  "malformed",
  "capability_mismatch",
  "policy_denied",
] as const;
const outcomeValues = [
  "success",
  "failure",
  "partial",
  "denied",
  "unknown",
  "cancelled",
  "timeout",
  "error",
  "skipped",
] as const;
const safetyLevels = ["unknown", "low", "medium", "high", "critical"] as const;
const policyFloors = ["none", "isolated", "protected", "standard", "best_effort", "medium", "high"] as const;

const workflowStepSchema = z.object({
  id: nonEmptyString.optional(),
  action: z.enum(workflowActions),
  objective: nonEmptyString.optional(),
  parallel: z.boolean().optional(),
  required: z.boolean().optional(),
  verification: nonEmptyString.optional(),
}).strict();
const workflowStepsSchema = z.array(workflowStepSchema).min(1);
const fallbackStepsSchema = z.array(workflowStepSchema);

const capabilityRequirementSchema = z.object({
  capability: nonEmptyString,
  required: z.boolean().default(true),
  minimum_level: nullableString.default(null),
  reason: nullableString.default(null),
  schema_version: schemaVersion.default("1"),
}).strict();

const taskSpecSchema = z.object({
  objective: nonEmptyString,
  task_type: nonEmptyString,
  effort: z.enum(["unknown", "low", "medium", "high"]).default("unknown"),
  reasoning: z.enum(["unknown", "low", "medium", "high"]).default("unknown"),
  capabilities: z.array(nonEmptyString).default([]),
  required_capabilities: z.array(nonEmptyString).default([]),
  tools: z.array(nonEmptyString).default([]),
  context: jsonObject.nullable().default(null),
  context_requirements: jsonObject.default({}),
  tool_requirements: z.record(z.string(), z.boolean()).default({}),
  privacy: z.enum(["unknown", "public", "internal", "trusted_upstream", "restricted"]).default("unknown"),
  risk: z.enum(safetyLevels).default("unknown"),
  budget: z.object({
    max_usd: nonNegativeNumber.optional(),
    estimated_usd: nonNegativeNumber.optional(),
    remaining_usd: nonNegativeNumber.optional(),
    estimated_tokens: nonNegativeInteger.optional(),
    estimated_latency_ms: nonNegativeInteger.optional(),
    estimate_basis: nonEmptyString.optional(),
  }).strict().default({}),
  latency: z.object({ max_ms: nonNegativeInteger.optional() }).strict().nullable().default(null),
  latency_limit_ms: nonNegativeInteger.nullable().default(null),
  workflow: z.object({ steps: workflowStepsSchema }).strict().nullable().default(null),
  approvals: z.array(nonEmptyString).default([]),
  criticality: z.enum(safetyLevels).default("unknown"),
  verification: jsonObject.nullable().default(null),
  parallelism: z.enum(["serial", "parallel", "bounded"]).default("serial"),
  destructive_operation: z.boolean().default(false),
  production_impact: z.boolean().default(false),
  degraded_mode_policy: z.enum(["deny", "allow", "allow_with_penalty"]).default("deny"),
  metadata: jsonObject.default({}),
  schema_version: schemaVersion.default("1"),
}).strict();

const runtimeCandidateSchema = z.object({
  runtime_id: nonEmptyString,
  catalog_present: z.boolean(),
  live_eligible: z.boolean(),
  availability: z.enum(availabilityStates),
  signals: z.record(z.string(), jsonObject),
  capabilities: z.array(z.string()).default([]),
  provider: nullableString.default(null),
  model: nullableString.default(null),
  model_version: nullableString.default(null),
  context_window: z.number().int().nonnegative().nullable().default(null),
  max_output_tokens: z.number().int().nonnegative().nullable().default(null),
  schema_version: schemaVersion.default("1"),
}).strict();

const verificationPlanSchema = z.object({
  checks: z.array(z.unknown()).default([]),
  on_failure: z.enum(["deny", "replan_or_deny"]).default("deny"),
  schema_version: schemaVersion.default("1"),
}).strict();

const workflowPlanSchema = z.object({
  steps: workflowStepsSchema,
  plan_id: nullableString.default(null),
  verification: verificationPlanSchema.default({}),
  verification_plan_id: nullableString.default(null),
  fallback_allowed: z.boolean().default(false),
  fallback_plan: fallbackStepsSchema.default([]),
  policy_version: z.string().default("1"),
  metadata: jsonObject.default({}),
  schema_version: schemaVersion.default("1"),
}).strict();

const availabilitySnapshotSchema = z.object({
  observed_at: z.string(),
  state: z.enum(availabilityStates).default("unknown"),
  signals: z.record(z.string(), jsonObject).default({}),
  candidates: z.array(runtimeCandidateSchema).default([]),
  source: z.string().default("unknown"),
  ttl_seconds: nonNegativeInteger.default(60),
  expires_at: nullableString.default(null),
  schema_version: schemaVersion.default("1"),
}).strict();

const routingDecisionSchema = z.object({
  selected_route: jsonObject,
  task_spec: jsonObject.default({}),
  candidate_snapshot: z.union([z.string(), jsonObject, z.null()]).default(null),
  exclusions: z.array(jsonObject).default([]),
  policy_floor: z.enum(policyFloors).default("none"),
  planner_mode: z.string().default("default"),
  explanation: z.string().default(""),
  adaptive_influence: jsonObject.default({}),
  fallback_plan: fallbackStepsSchema.default([]),
  correlation_id: nullableString.default(null),
  request_id: nullableString.default(null),
  policy_version: z.string().default("1"),
  schema_version: schemaVersion.default("1"),
}).strict();

const fallbackAttemptSchema = z.object({
  runtime_id: nonEmptyString,
  reason: nonEmptyString,
  legal: z.boolean().default(true),
  schema_version: schemaVersion.default("1"),
}).strict();

const outcomeEventSchema = z.object({
  event_id: nullableString.default(null),
  event_type: nonEmptyString,
  correlation_id: nullableString.default(null),
  outcome: z.enum(outcomeValues),
  occurred_at: nonEmptyString,
  request_id: nullableString.default(null),
  verification: jsonObject.default({}),
  quality: jsonObject.default({}),
  latency_ms: nonNegativeNumber.nullable().default(null),
  cost: jsonObject.default({}),
  retries: nonNegativeInteger.default(0),
  fallbacks: z.array(z.unknown()).default([]),
  provider_version: nullableString.default(null),
  model_version: nullableString.default(null),
  details: jsonObject.nullable().default(null),
  schema_version: schemaVersion.default("1"),
}).strict();

const taskEpisodeSchema = z.object({
  task_fingerprint: nonEmptyString,
  objective_preview: nonEmptyString,
  task_type: z.string().default("unknown"),
  privacy: z.string().default("unknown"),
  risk: z.string().default("unknown"),
  required_capabilities: z.array(z.string()).default([]),
  tools: z.array(z.string()).default([]),
  approvals_count: nonNegativeInteger.default(0),
  context_keys: z.array(z.string()).default([]),
  metadata: jsonObject.default({}),
  schema_version: schemaVersion.default("1"),
}).strict();

const workflowEpisodeSchema = z.object({
  workflow_fingerprint: nonEmptyString,
  plan_id: nullableString.default(null),
  step_count: nonNegativeInteger.default(0),
  fallback_allowed: z.boolean().default(false),
  fallback_step_count: nonNegativeInteger.default(0),
  verification_checks: z.array(z.string()).default([]),
  verification_plan_id: nullableString.default(null),
  policy_version: z.string().default("1"),
  metadata: jsonObject.default({}),
  schema_version: schemaVersion.default("1"),
}).strict();

const outcomeEpisodeSchema = z.object({
  outcome_fingerprint: nonEmptyString,
  event_type: nullableString.default(null),
  outcome: z.enum(outcomeValues).nullable().default(null),
  occurred_at: nullableString.default(null),
  request_id: nullableString.default(null),
  correlation_id: nullableString.default(null),
  decision: nullableString.default(null),
  policy_floor: nullableString.default(null),
  selected_route: jsonObject.default({}),
  verification: jsonObject.default({}),
  quality: jsonObject.default({}),
  latency_ms: nonNegativeNumber.nullable().default(null),
  cost: jsonObject.default({}),
  retries: nonNegativeInteger.default(0),
  fallback_count: nonNegativeInteger.default(0),
  provider_version: nullableString.default(null),
  model_version: nullableString.default(null),
  details: jsonObject.nullable().default(null),
  schema_version: schemaVersion.default("1"),
}).strict();

const taskWorkflowOutcomeEpisodeSchema = z.object({
  episode_id: nullableString.default(null),
  request_id: nullableString.default(null),
  correlation_id: nullableString.default(null),
  policy_version: z.string().default("1"),
  task: z.union([taskEpisodeSchema, jsonObject]).default({}),
  workflow: z.union([workflowEpisodeSchema, jsonObject]).default({}),
  outcome: z.union([outcomeEpisodeSchema, jsonObject]).default({}),
  schema_version: schemaVersion.default("1"),
}).strict();

const learningEventSchema = z.object({
  event_id: nullableString.default(null),
  signal: z.string().default(""),
  correlation_id: nullableString.default(null),
  value: z.unknown().default(null),
  occurred_at: nullableString.default(null),
  evidence: jsonObject.default({}),
  metadata: jsonObject.default({}),
  request_id: nullableString.default(null),
  schema_version: schemaVersion.default("1"),
}).strict();

const schemas = {
  task_spec: taskSpecSchema,
  TaskSpec: taskSpecSchema,
  capability_requirement: capabilityRequirementSchema,
  CapabilityRequirement: capabilityRequirementSchema,
  runtime_candidate: runtimeCandidateSchema,
  RuntimeCandidate: runtimeCandidateSchema,
  availability_snapshot: availabilitySnapshotSchema,
  AvailabilitySnapshot: availabilitySnapshotSchema,
  workflow_plan: workflowPlanSchema,
  WorkflowPlan: workflowPlanSchema,
  routing_decision: routingDecisionSchema,
  RoutingDecision: routingDecisionSchema,
  RoutingDecisionContract: routingDecisionSchema,
  fallback_attempt: fallbackAttemptSchema,
  FallbackAttempt: fallbackAttemptSchema,
  verification_plan: verificationPlanSchema,
  VerificationPlan: verificationPlanSchema,
  task_episode: taskEpisodeSchema,
  TaskEpisode: taskEpisodeSchema,
  workflow_episode: workflowEpisodeSchema,
  WorkflowEpisode: workflowEpisodeSchema,
  outcome_episode: outcomeEpisodeSchema,
  OutcomeEpisode: outcomeEpisodeSchema,
  task_workflow_outcome_episode: taskWorkflowOutcomeEpisodeSchema,
  TaskWorkflowOutcomeEpisode: taskWorkflowOutcomeEpisodeSchema,
  outcome_event: outcomeEventSchema,
  OutcomeEvent: outcomeEventSchema,
  learning_event: learningEventSchema,
  LearningEvent: learningEventSchema,
} as const;

export type ContractName = keyof typeof schemas;
export type TaskSpec = z.output<typeof taskSpecSchema>;
export type CapabilityRequirement = z.output<typeof capabilityRequirementSchema>;
export type RuntimeCandidate = z.output<typeof runtimeCandidateSchema>;
export type AvailabilitySnapshot = z.output<typeof availabilitySnapshotSchema>;
export type VerificationPlan = z.output<typeof verificationPlanSchema>;
export type WorkflowPlan = z.output<typeof workflowPlanSchema>;
export type RoutingDecision = z.output<typeof routingDecisionSchema>;
export type FallbackAttempt = z.output<typeof fallbackAttemptSchema>;
export type OutcomeEvent = z.output<typeof outcomeEventSchema>;
export type TaskEpisode = z.output<typeof taskEpisodeSchema>;
export type WorkflowEpisode = z.output<typeof workflowEpisodeSchema>;
export type OutcomeEpisode = z.output<typeof outcomeEpisodeSchema>;
export type TaskWorkflowOutcomeEpisode = z.output<typeof taskWorkflowOutcomeEpisodeSchema>;
export type LearningEvent = z.output<typeof learningEventSchema>;

function errorCategory(error: ZodError, path: readonly (string | number)[]): ContractErrorCategory {
  const issue = error.issues[0];
  if (!issue) return "invalid_value";
  const issuePath = [...path, ...issue.path];
  if (issuePath.includes("schema_version")) return "schema_version";
  if (issue.code === "unrecognized_keys") return "unknown_field";
  if (issue.code === "invalid_type" && issue.received === "undefined") {
    return "missing_field";
  }
  if (issuePath.includes("action")) return "unsafe_workflow";
  if (issue.code === "invalid_type" || issue.code === "invalid_union") return "invalid_type";
  return "invalid_value";
}

function formatZodError(error: ZodError): string {
  const issue = error.issues[0];
  if (!issue) return "invalid contract";
  const path = issue.path.length ? issue.path.join(".") : "contract";
  return `${path}: ${issue.message}`;
}

function parseWithSchema<T>(schema: ZodType<T>, value: unknown, name: string): T {
  try {
    const result = schema.safeParse(value);
    if (result.success) return result.data;
    throw new ContractValidationError(
      errorCategory(result.error, []),
      `${name}: ${formatZodError(result.error)}`,
      result.error.issues[0]?.path ?? [],
    );
  } catch (error) {
    if (error instanceof ContractValidationError) throw error;
    throw new ContractValidationError("invalid_type", `${name}: contract must be a JSON object`);
  }
}

/** Parse a strict v1 contract, applying the Python-compatible v1 defaults. */
export function parseContract<N extends ContractName>(name: N, value: unknown): z.output<(typeof schemas)[N]> {
  const schema = schemas[name];
  if (!schema) {
    throw new ContractValidationError("invalid_value", `unknown contract: ${String(name)}`);
  }
  const redactionAllowed = name === "OutcomeEvent" || name === "outcome_event";
  const input = redactionAllowed && value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>), details: redactContractSecrets((value as Record<string, unknown>).details) }
    : value;
  rejectSecrets(input);
  const parsed = parseWithSchema(schema as ZodType<z.output<(typeof schemas)[N]>>, input, String(name));
  return parsed;
}

function runtimeId(provider: unknown, model: unknown): string | undefined {
  return typeof provider === "string" && provider && typeof model === "string" && model
    ? `${provider}/${model}`
    : undefined;
}

function policyFloorFromTier(tier: unknown): string {
  return ({ 0: "isolated", 1: "protected", 2: "standard", 3: "best_effort" } as Record<number, string>)[tier as number] ?? "none";
}

/** Explicitly migrate the supported pre-v1 payloads; never use this for v1 input. */
export function parseLegacyContract(name: "task_spec" | "routing_decision", value: unknown): TaskSpec | RoutingDecision {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ContractValidationError("invalid_type", "legacy contract must be a JSON object");
  }
  const legacy = { ...(value as Record<string, unknown>) };
  if (name === "task_spec") {
    const objective = legacy.task ?? legacy.objective;
    if (typeof objective !== "string") {
      throw new ContractValidationError("missing_field", "legacy task contract requires 'task' or 'objective'");
    }
    delete legacy.task;
    delete legacy.objective;
    const metadata = legacy.metadata && typeof legacy.metadata === "object" && !Array.isArray(legacy.metadata)
      ? { ...(legacy.metadata as Record<string, unknown>) }
      : {};
    const mapped: Record<string, unknown> = {
      objective,
      task_type: legacy.task_type ?? "unknown",
      criticality: legacy.criticality ?? "unknown",
      context: legacy.context ?? null,
      metadata,
    };
    delete legacy.task_type;
    delete legacy.criticality;
    delete legacy.context;
    delete legacy.metadata;
    if (Object.keys(legacy).length) metadata.legacy = legacy;
    return parseContract("task_spec", mapped);
  }
  const provider = legacy.provider;
  const model = legacy.model;
  const selectedRoute: Record<string, unknown> = {};
  const selectedKeys = ["headroom_pct", "latency_ms", "decision", "escalated", "escalation_reason", "logged", "task_class"];
  for (const key of selectedKeys) if (key in legacy) { selectedRoute[key] = legacy[key]; delete legacy[key]; }
  selectedRoute.runtime_id = legacy.runtime_id ?? runtimeId(provider, model);
  if (provider !== undefined) selectedRoute.provider = provider;
  if (model !== undefined) selectedRoute.model = model;
  for (const key of ["runtime_id", "provider", "model"]) delete legacy[key];
  const alternatives = Array.isArray(legacy.alternatives) ? legacy.alternatives : [];
  delete legacy.alternatives;
  const exclusions = alternatives.map((item) => typeof item === "object" && item !== null
    ? item as Record<string, unknown>
    : { model: String(item), reason: "legacy alternative" });
  const adaptiveInfluence = legacy.adaptive_influence && typeof legacy.adaptive_influence === "object" && !Array.isArray(legacy.adaptive_influence)
    ? { ...(legacy.adaptive_influence as Record<string, unknown>) }
    : {};
  delete legacy.adaptive_influence;
  if (Object.keys(legacy).length) adaptiveInfluence.legacy = legacy;
  const mapped = {
    selected_route: selectedRoute,
    task_spec: legacy.task_spec ?? {},
    candidate_snapshot: legacy.candidate_snapshot ?? null,
    exclusions,
    policy_floor: legacy.policy_floor ?? policyFloorFromTier(legacy.tier),
    planner_mode: legacy.planner_mode ?? "legacy-routing-decision",
    explanation: legacy.reason ?? "",
    adaptive_influence: adaptiveInfluence,
    fallback_plan: legacy.fallback_plan ?? [],
    correlation_id: legacy.correlation_id ?? null,
    request_id: legacy.request_id ?? null,
    policy_version: String(legacy.policy_version ?? "1"),
  };
  for (const key of [
    "task_spec",
    "candidate_snapshot",
    "policy_floor",
    "tier",
    "planner_mode",
    "reason",
    "fallback_plan",
    "correlation_id",
    "request_id",
    "policy_version",
  ]) delete legacy[key];
  return parseContract("routing_decision", mapped);
}

/** Serialize a previously validated contract or a JSON object without secrets. */
export function serializeContract(value: JsonObject): string;
export function serializeContract<N extends ContractName>(name: N, value: unknown): string;
export function serializeContract(first: string | JsonObject, second?: unknown): string {
  if (typeof first === "string") return JSON.stringify(parseContract(first as ContractName, second));
  rejectSecrets(first);
  return JSON.stringify(first);
}

export const contractSchemas = schemas;
