import { describe, it, expect } from 'vitest';
import {
  parseContract,
  serializeContract,
  redactContractSecrets,
  ContractValidationError,
  ContractErrorCategory,
  type TaskSpec,
  type RoutingDecision,
  type AvailabilitySnapshot,
} from "../src/index.js";

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
      const nested = (redacted as Record<string, unknown>).nested as Record<string, unknown>;
      expect(nested.authorization).toBe("[redacted]");
      expect(nested.public_data).toBe("visible");
    });
  });
});
