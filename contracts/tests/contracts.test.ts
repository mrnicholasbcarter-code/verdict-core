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
  type EvidenceReceipt,
  type ModelPassport,
  type VerificationResult,
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

  describe("EvidenceReceipt", () => {
    it("should preserve exact route identity and evidence authority", () => {
      const receipt: EvidenceReceipt = {
        schema_version: "1",
        receipt_id: "receipt-1",
        kind: "decision",
        scope: "project:test",
        occurred_at: "2026-07-30T00:00:00Z",
        requested_alias: "nvidia/model:free",
        selected_route: {
          gateway: "omniroute/instance-a",
          provider: "nvidia",
          connection: "team-free",
          endpoint: "https://gateway.example/v1/responses",
          protocol: "openai.responses",
          model_id: "nvidia/model",
          transformation_chain: ["responses-http"],
          fallback_chain: [],
        },
        evidence: [
          {
            authority: "verified",
            source: "verdict:fixture",
            method: "hermetic-probe",
            adapter_version: "adapter-1",
            observed_at: "2026-07-30T00:00:00Z",
            expires_at: "2026-07-30T00:05:00Z",
            scope: "project:test",
            confidence: 1,
            evidence_digest: `sha256:${"a".repeat(64)}`,
            limitations: [],
            sample_count: 1,
          },
        ],
        payload: { route_identity: { gateway: "omniroute/instance-a", protocol: "openai.responses" } },
        parent_receipt_ids: [],
        extensions: { future_field: { version: 2 } },
      };

      const parsed = parseContract("EvidenceReceipt", receipt);
      expect(parsed).toEqual(receipt);
      expect(serializeContract("EvidenceReceipt", parsed)).toContain('"authority":"verified"');
    });

    it("should reject unknown receipt fields and preserve fail-closed boundaries", () => {
      expect(() =>
        parseContract("evidence_receipt", {
          receipt_id: "receipt-1",
          kind: "decision",
          scope: "project:test",
          occurred_at: "2026-07-30T00:00:00Z",
          evidence: [],
          payload: {},
          parent_receipt_ids: [],
          extensions: {},
          unexpected: true,
        }),
      ).toThrow(ContractValidationError);
    });

    it("should reject prompt content in receipt metadata", () => {
      const receipt = {
        receipt_id: "receipt-2",
        kind: "outcome",
        scope: "project:test",
        occurred_at: "2026-07-30T00:00:00Z",
        evidence: [
          {
            authority: "observed",
            source: "fixture",
            method: "probe",
            adapter_version: "1",
            observed_at: "2026-07-30T00:00:00Z",
            expires_at: "2026-07-30T00:05:00Z",
            scope: "project:test",
            confidence: 1,
            evidence_digest: `sha256:${"a".repeat(64)}`,
            limitations: [],
          },
        ],
        payload: { raw_prompt: "must not persist" },
        parent_receipt_ids: [],
        extensions: {},
      };

      expect(() => parseContract("evidence_receipt", receipt)).toThrow(ContractValidationError);
    });
  });

  describe("ModelPassport", () => {
    it("should parse a valid model passport", () => {
      const passport: ModelPassport = {
        schema_version: "1",
        provider: "openai",
        model_id: "gpt-4o",
        auth_state: "authorized",
        last_verified_timestamp: "2026-07-30T00:00:00Z",
        availability_state: "eligible",
        qualified_at: "2026-07-30T00:00:00Z",
        expires_at: "2026-07-30T00:05:00Z",
        latency_p95: 123.4,
        context_window: 128000,
        tool_support: true,
        token_cost_per_1k: 0.005,
        availability_reason: "verified",
        recovery_attempts: 0,
      };

      const result = parseContract("model_passport", passport);
      expect(result).toEqual(passport);
      expect(result.schema_version).toBe("1");
    });

    it("should parse a quarantined passport with quarantine timestamps", () => {
      const passport = {
        schema_version: "1",
        provider: "openai",
        model_id: "gpt-4o",
        auth_state: "unauthorized",
        last_verified_timestamp: "2026-07-30T00:00:00Z",
        availability_state: "quarantined",
        qualified_at: "2026-07-30T00:00:00Z",
        expires_at: "2026-07-30T00:05:00Z",
        quarantine_until: "2026-07-30T00:30:00Z",
        quarantined_at: "2026-07-30T00:00:00Z",
        recovery_attempts: 1,
      };

      const result = parseContract("ModelPassport", passport);
      expect(result.availability_state).toBe("quarantined");
      expect(result.quarantine_until).toBe("2026-07-30T00:30:00Z");
    });

    it("should reject unknown fields in a model passport", () => {
      const passport = {
        schema_version: "1",
        provider: "openai",
        model_id: "gpt-4o",
        auth_state: "authorized",
        last_verified_timestamp: "2026-07-30T00:00:00Z",
        availability_state: "eligible",
        qualified_at: "2026-07-30T00:00:00Z",
        expires_at: "2026-07-30T00:05:00Z",
        unexpected: true,
      };

      expect(() => parseContract("model_passport", passport)).toThrow(ContractValidationError);
    });

    it("should reject a non-'1' schema version", () => {
      const passport = {
        schema_version: "2",
        provider: "openai",
        model_id: "gpt-4o",
        auth_state: "authorized",
        last_verified_timestamp: "2026-07-30T00:00:00Z",
        availability_state: "eligible",
        qualified_at: "2026-07-30T00:00:00Z",
        expires_at: "2026-07-30T00:05:00Z",
      };

      expect(() => parseContract("model_passport", passport)).toThrow(ContractValidationError);
    });
  });

  describe("VerificationResult", () => {
    const validResult: VerificationResult = {
      check_name: "focused-tests",
      check_type: "focused_tests",
      status: "passed",
      details: { selected: 12, suite: "tests/test_contracts.py" },
      artifact_digests: [`sha256:${"3f".repeat(32)}`],
      duration_ms: 1200,
      command: "python -m pytest -q tests/test_contracts.py",
      runtime: "cpython-3.11.9/linux-x86_64",
      provenance: "verdict-core.ci/github-actions",
      policy_requirement: "VER-009:focused-tests-must-pass",
      raw_output: "12 passed in 1.20s",
      schema_version: "1",
    };

    it("should parse a result carrying full re-run provenance", () => {
      const parsed = parseContract("verification_result", validResult) as VerificationResult;

      expect(parsed.command).toBe("python -m pytest -q tests/test_contracts.py");
      expect(parsed.runtime).toBe("cpython-3.11.9/linux-x86_64");
      expect(parsed.provenance).toBe("verdict-core.ci/github-actions");
      expect(parsed.policy_requirement).toBe("VER-009:focused-tests-must-pass");
      expect(JSON.parse(serializeContract(parsed))).toEqual(validResult);
    });

    it("should keep unknown distinct from passed and failed", () => {
      const parsed = parseContract("verification_result", {
        check_name: "coverage",
        check_type: "ci",
        status: "unknown",
      }) as VerificationResult;

      expect(parsed.status).toBe("unknown");
      expect(["passed", "failed"]).not.toContain(parsed.status);
    });

    it("should reject secret-bearing raw output", () => {
      expect(() =>
        parseContract("verification_result", {
          ...validResult,
          raw_output: "Authorization: Bearer sk-live-DEADBEEF9182",
        })
      ).toThrow(ContractValidationError);
    });

    it("should reject an unknown status", () => {
      expect(() =>
        parseContract("verification_result", { ...validResult, status: "green" })
      ).toThrow(ContractValidationError);
    });

    it("should reject a malformed artifact digest", () => {
      expect(() =>
        parseContract("verification_result", {
          ...validResult,
          artifact_digests: ["sha1:deadbeef"],
        })
      ).toThrow(ContractValidationError);
    });

    it("should reject a negative duration", () => {
      expect(() =>
        parseContract("verification_result", { ...validResult, duration_ms: -1 })
      ).toThrow(ContractValidationError);
    });

    it("should reject unknown fields", () => {
      expect(() =>
        parseContract("verification_result", { ...validResult, unexpected: true })
      ).toThrow(ContractValidationError);
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
