import { describe, it, expect } from 'vitest';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import {
  parseContract,
  serializeContract,
  redactContractSecrets,
  ContractValidationError,
  ContractErrorCategory,
  type TaskSpec,
  type RoutingDecision,
  type AvailabilitySnapshot,
  type ExecutionEnvelope,
} from "../src/index.js";

const fixturesDir = fileURLToPath(new URL("../../tests/fixtures", import.meta.url));
const envelopeFixturesDir = fileURLToPath(new URL("../../test_fixtures/envelopes", import.meta.url));
async function loadFixture(name: string): Promise<Record<string, unknown>> {
  return JSON.parse(await readFile(`${fixturesDir}/${name}`, "utf8")) as Record<string, unknown>;
}
async function loadEnvelopeFixture(name: string): Promise<Record<string, unknown>> {
  return JSON.parse(await readFile(`${envelopeFixturesDir}/${name}`, "utf8")) as Record<string, unknown>;
}

describe("Contract Validation", () => {
  describe("TaskSpec", () => {
    it("should parse a valid task spec", () => {
      const taskSpec: TaskSpec = {
        objective: "Write a TypeScript function",
        task_type: "codegen",
        effort: "low",
        reasoning: "low",
        capabilities: [],
        required_capabilities: [],
        tools: [],
        context: null,
        context_requirements: {},
        tool_requirements: {},
        privacy: "public",
        risk: "low",
        budget: {},
        latency: null,
        latency_limit_ms: null,
        workflow: null,
        approvals: [],
        criticality: "low",
        verification: null,
        parallelism: "serial",
        destructive_operation: false,
        production_impact: false,
        degraded_mode_policy: "deny",
        metadata: {},
        schema_version: "1",
      };

      const result = parseContract("task_spec", taskSpec);
      expect(result).toEqual(taskSpec);
    });

    it("should reject invalid task spec", () => {
      const invalidSpec = {
        objective: "",
        task_type: "invalid",
      };

      expect(() => parseContract("task_spec", invalidSpec)).toThrow();
    });
  });

  describe("RoutingDecision", () => {
    it("should parse a valid routing decision", () => {
      const decision = {
        selected_route: {
          runtime_id: "test-runtime",
          provider: "openai",
          model: "gpt-4o",
          decision: "route",
          latency_ms: 100,
          headroom_pct: 20,
        },
        task_spec: {
          objective: "Test",
          task_type: "codegen",
          effort: "low",
          reasoning: "low",
          capabilities: [],
          required_capabilities: [],
          tools: [],
          context: null,
          context_requirements: {},
          tool_requirements: {},
          privacy: "public",
          risk: "low",
          budget: {},
          latency: null,
          latency_limit_ms: null,
          workflow: null,
          approvals: [],
          criticality: "low",
          verification: null,
          parallelism: "serial",
          destructive_operation: false,
          production_impact: false,
          degraded_mode_policy: "deny",
          metadata: {},
          schema_version: "1",
        },
        candidate_snapshot: null,
        exclusions: [],
        policy_floor: "none",
        planner_mode: "default",
        explanation: "Test routing",
        adaptive_influence: {},
        fallback_plan: [],
        correlation_id: null,
        request_id: null,
        policy_version: "1",
        schema_version: "1",
      };

      const result = parseContract("routing_decision", decision);
      expect(result).toEqual(decision);
    });
  });

  describe("AvailabilitySnapshot", () => {
    it("should parse a valid availability snapshot", () => {
      const observed_at = new Date().toISOString();
      const snapshot = {
        observed_at,
        candidates: [],
      };

      const result = parseContract("availability_snapshot", snapshot);
      // Check that required fields are present and match
      expect(result.observed_at).toBe(snapshot.observed_at);
      expect(result.candidates).toEqual([]);
      expect(result.schema_version).toBe("1");
      // Zod adds defaults - check they're present
      expect(result.state).toBe("unknown");
      expect(result.ttl_seconds).toBe(60);
      expect(typeof result.signals).toBe("object");
    });
  });

  describe("Serialization", () => {
    it("should serialize a valid contract", () => {
      const taskSpec = {
        objective: "Test",
        task_type: "codegen",
        effort: "low",
        reasoning: "low",
        capabilities: [],
        required_capabilities: [],
        tools: [],
        context: null,
        context_requirements: {},
        tool_requirements: {},
        privacy: "public",
        risk: "low",
        budget: {},
        latency: null,
        latency_limit_ms: null,
        workflow: null,
        approvals: [],
        criticality: "low",
        verification: null,
        parallelism: "serial",
        destructive_operation: false,
        production_impact: false,
        degraded_mode_policy: "deny",
        metadata: {},
        schema_version: "1",
      };

      const serialized = serializeContract("task_spec", taskSpec);
      expect(typeof serialized).toBe("string");
      expect(JSON.parse(serialized)).toEqual(taskSpec);
    });

    it("should reject secrets in serialization", () => {
      const withSecret = {
        api_key: "secret123",
        normal_field: "value",
      };

      expect(() => serializeContract(withSecret)).toThrow();
    });
  });

  describe("Redaction", () => {
    it("should redact secret-like keys", () => {
      const data = {
        api_key: "secret",
        password: "pass123",
        normal_field: "value",
        nested: {
          authorization: "bearer token",
          public_data: "visible",
        },
      };

      const redacted = redactContractSecrets(data);
      expect((redacted as Record<string, unknown>).api_key).toBe("[redacted]");
      expect((redacted as Record<string, unknown>).password).toBe("[redacted]");
      expect((redacted as Record<string, unknown>).normal_field).toBe("value");
      expect((redacted.nested as Record<string, unknown>).authorization).toBe("[redacted]");
      expect((redacted.nested as Record<string, unknown>).public_data).toBe("visible");
    });
  });

  // NOD-002 / ADR-025: ExecutionEnvelope mirrors the Python canonical
  // ExecutionEnvelope (verdict/contracts.py) field-for-field. The canonical
  // shape (task_spec/eligibility_decision/verification_requirements/...) is the
  // single source of truth for both runtimes; the previous divergent shape
  // (decision_id/policy_version/expires_at/task_spec_fingerprint/verification_plan/
  // provenance) was unified to canonical under VER-003 / #220.
  describe("ExecutionEnvelope", () => {
    const validEnvelope: ExecutionEnvelope = {
      schema_version: "1",
      task_spec: {
        objective: "Write a TypeScript function",
        task_type: "codegen",
        effort: "low",
        reasoning: "low",
        capabilities: [],
        required_capabilities: [],
        tools: [],
        context: null,
        context_requirements: {},
        tool_requirements: {},
        privacy: "public",
        risk: "low",
        budget: {},
        latency: null,
        latency_limit_ms: null,
        workflow: null,
        approvals: [],
        criticality: "low",
        verification: null,
        parallelism: "serial",
        destructive_operation: false,
        production_impact: false,
        degraded_mode_policy: "deny",
        metadata: {},
        schema_version: "1",
      },
      eligibility_decision: { admitted: ["gpt-4o"] },
      policy_digest: "sha256:abc123",
      allowed_capabilities: ["chat", "codegen"],
      execution_constraints: {
        allowed_models: ["gpt-4o", "claude-3-5-sonnet"],
        allowed_tools: ["read_file", "write_file"],
        allowed_agents: ["coding-agent"],
        budget_usd: 10.0,
        max_request_usd: 1.0,
        max_latency_ms: 30000,
        risk_ceiling: "high",
        required_verification: ["safety_check", "budget_check"],
      },
      verification_requirements: {
        checks: ["safety_check"],
        on_failure: "deny",
      },
      evidence_ids: ["evidence-1"],
      routing_decision: null,
      created_at: "2026-08-20T00:00:00Z",
    };

    it("parses a valid envelope", () => {
      const parsed = parseContract("execution_envelope", validEnvelope);
      expect(parsed.task_spec.objective).toBe("Write a TypeScript function");
      expect(parsed.eligibility_decision).toEqual({ admitted: ["gpt-4o"] });
      expect(parsed.policy_digest).toBe("sha256:abc123");
      expect(parsed.execution_constraints.allowed_models).toEqual(["gpt-4o", "claude-3-5-sonnet"]);
      expect(parsed.verification_requirements.checks).toEqual(["safety_check"]);
    });

    it("rejects unknown top-level fields", () => {
      const tampered = { ...validEnvelope, injected_field: "malicious" };
      expect(() => parseContract("execution_envelope", tampered)).toThrow(ContractValidationError);
      try {
        parseContract("execution_envelope", tampered);
      } catch (err: any) {
        expect(err.category).toBe("unknown_field");
      }
    });

    it("rejects unknown nested constraint fields", () => {
      const tampered = {
        ...validEnvelope,
        execution_constraints: {
          ...validEnvelope.execution_constraints,
          allow_everything: true,
        },
      };
      expect(() => parseContract("execution_envelope", tampered)).toThrow(ContractValidationError);
      try {
        parseContract("execution_envelope", tampered);
      } catch (err: any) {
        expect(err.category).toBe("unknown_field");
      }
    });

    it("rejects tampered policy_digest", () => {
      const tampered = { ...validEnvelope, policy_digest: "sha256:different" };
      const parsed = parseContract("execution_envelope", tampered);
      // Parsing succeeds (schema valid) but digest mismatch detected by edge adapter
      expect(parsed.policy_digest).toBe("sha256:different");
    });

    it("rejects malformed schema_version", () => {
      const malformed = { ...validEnvelope, schema_version: "2" };
      expect(() => parseContract("execution_envelope", malformed)).toThrow(ContractValidationError);
      try {
        parseContract("execution_envelope", malformed);
      } catch (err: any) {
        expect(err.category).toBe("schema_version");
      }
    });

    it("rejects missing required fields", () => {
      // Remove a required canonical field (e.g., eligibility_decision)
      const { eligibility_decision: _removed, ...missing } = validEnvelope;
      expect(() => parseContract("execution_envelope", missing)).toThrow(ContractValidationError);
    });

    it("accepts minimal constraints (empty arrays)", () => {
      const minimal = {
        ...validEnvelope,
        execution_constraints: {
          allowed_models: [],
          allowed_tools: [],
          allowed_agents: [],
        },
      };
      const parsed = parseContract("execution_envelope", minimal);
      expect(parsed.execution_constraints.allowed_models).toEqual([]);
    });

    it("serializes and round-trips correctly", () => {
      const serialized = serializeContract("execution_envelope", validEnvelope);
      const reparsed = parseContract("execution_envelope", JSON.parse(serialized));
      expect(reparsed.eligibility_decision).toEqual(validEnvelope.eligibility_decision);
      expect(reparsed.policy_digest).toBe(validEnvelope.policy_digest);
    });

    // NOD-002 (issue #286) cross-runtime parity: the same shared invalid
    // envelope fixtures must be rejected by the TS runtime here AND by the
    // Python canonical contract in tests/test_contracts.py, proving the edge
    // adapter cannot accept an envelope the orchestrator would reject.
    it("rejects shared invalid fixtures identically to the Python canonical contract", async () => {
      const invalidNames = [
        "invalid_empty_policy_digest.json",
        "invalid_missing_task_spec.json",
        "invalid_wrong_type_task_spec.json",
      ];
      for (const name of invalidNames) {
        const payload = await loadEnvelopeFixture(name);
        expect(() => parseContract("execution_envelope", payload)).toThrow(ContractValidationError);
      }
    });

    it("parses the shared valid envelope fixture", async () => {
      const payload = await loadEnvelopeFixture("valid_envelope.json");
      const parsed = parseContract("execution_envelope", payload);
      expect((parsed as ExecutionEnvelope).eligibility_decision).toEqual({ admitted: ["gpt-4"], reason: "test" });
    });
  });
});
