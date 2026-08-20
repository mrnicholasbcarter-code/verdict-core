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
async function loadFixture(name: string): Promise<Record<string, unknown>> {
  return JSON.parse(await readFile(`${fixturesDir}/${name}`, "utf8")) as Record<string, unknown>;
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

  describe("ExecutionEnvelope", () => {
    const validEnvelope: ExecutionEnvelope = {
      schema_version: "1",
      decision_id: "dec-123",
      policy_version: "1",
      policy_digest: "sha256:abc123",
      expires_at: new Date(Date.now() + 3600000).toISOString(),
      task_spec_fingerprint: "task-456",
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
      verification_plan: {
        required_checks: ["safety_check"],
        evidence_refs: ["evidence-1"],
        quality_gates: ["quality_gate_1"],
      },
      provenance: {
        core_version: "0.1.0",
        policy_fingerprint: "sha256:policy123",
        issued_at: new Date().toISOString(),
        issued_by: "core-router",
      },
      schema_version: "1",
    };

    it("parses a valid envelope", () => {
      const parsed = parseContract("execution_envelope", validEnvelope);
      expect(parsed.decision_id).toBe("dec-123");
      expect(parsed.policy_digest).toBe("sha256:abc123");
      expect(parsed.execution_constraints.allowed_models).toEqual(["gpt-4o", "claude-3-5-sonnet"]);
      expect(parsed.provenance.core_version).toBe("0.1.0");
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

    it("rejects expired envelope", () => {
      const expired = { ...validEnvelope, expires_at: new Date(Date.now() - 1000).toISOString() };
      const parsed = parseContract("execution_envelope", expired);
      // Schema parsing succeeds; expiry check done by edge adapter
      expect(parsed.expires_at).toBe(expired.expires_at);
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
      const { decision_id: _removed, ...missing } = validEnvelope;
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
      expect(reparsed.decision_id).toBe(validEnvelope.decision_id);
      expect(reparsed.policy_digest).toBe(validEnvelope.policy_digest);
    });

    // NOD-002 (issue #286) cross-runtime parity: the same shared invalid
    // envelope fixtures must be rejected by the TS runtime here AND by the
    // Python canonical contract in tests/test_contracts.py, proving the edge
    // adapter cannot accept an envelope the orchestrator would reject.
    it("rejects shared invalid fixtures identically to the Python canonical contract", async () => {
      const invalidNames = [
        "invalid-envelope-missing-policy-digest.json",
        "invalid-envelope-empty-decision-id.json",
        "invalid-envelope-wrong-schema-version.json",
      ];
      for (const name of invalidNames) {
        const payload = await loadFixture(name);
        expect(() => parseContract("execution_envelope", payload)).toThrow(ContractValidationError);
      }
    });

    it("parses the shared valid envelope fixture", async () => {
      const payload = await loadFixture("envelope-valid.json");
      const parsed = parseContract("execution_envelope", payload);
      expect((parsed as ExecutionEnvelope).decision_id).toBe("dec-001");
    });
  });
});
