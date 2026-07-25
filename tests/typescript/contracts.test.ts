import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  ContractValidationError,
  parseContract,
  parseLegacyContract,
  redactContractSecrets,
  serializeContract,
} from "../../contracts/src/index.ts";

type TaskContract = import("../../contracts/index.ts").TaskSpec;
type RoutingContract = import("../../contracts/index.ts").RoutingDecision;

const root = fileURLToPath(new URL("../..", import.meta.url));

async function fixture(name: string): Promise<Record<string, unknown>> {
  return JSON.parse(await readFile(`${root}/tests/fixtures/${name}`, "utf8")) as Record<string, unknown>;
}

test("every canonical valid fixture parses and serializes semantically", async () => {
  const payload = await fixture("contract-v1.json");
  for (const [name, value] of Object.entries(payload)) {
    const parsed = parseContract(name as Parameters<typeof parseContract>[0], value);
    assert.deepEqual(JSON.parse(serializeContract(parsed)), value, name);
  }
});

test("canonical invalid fixtures fail with the same categories as Python", async () => {
  const cases: Array<[string, string]> = [
    ["invalid-missing-objective.json", "missing_field"],
    ["invalid-capability-type.json", "invalid_type"],
    ["invalid-budget.json", "invalid_value"],
    ["invalid-schema-version.json", "schema_version"],
    ["invalid-unknown-field.json", "unknown_field"],
    ["invalid-unsafe-workflow.json", "unsafe_workflow"],
  ];
  for (const [name, category] of cases) {
    await assert.rejects(
      () => fixture(name).then((value) => parseContract("task_spec", value)),
      (error: unknown) => error instanceof ContractValidationError && error.category === category,
    );
  }
});

test("secret-bearing fields fail closed and redaction is structural", () => {
  assert.throws(
    () => parseContract("task_spec", { objective: "ship", task_type: "coding", context: { api_key: "secret" } }),
    (error: unknown) => error instanceof ContractValidationError && error.category === "secret_bearing",
  );
  assert.deepEqual(
    redactContractSecrets({
      metadata: { api_key: "secret" },
      prompt: "private prompt",
      context: { authorization: "Bearer secret", url: "https://user:password@example.test/?token=secret" },
    }),
    {
      metadata: { api_key: "[redacted]" },
      prompt: "[redacted]",
      context: { authorization: "[redacted]", url: "https://[redacted]@example.test/?token=[redacted]" },
    },
  );
});

test("legacy task and routing shapes require explicit migration", () => {
  const task = parseLegacyContract("task_spec", {
    task: "Ship the contracts migration",
    criticality: "high",
    custom_hint: "preserve under metadata",
  });
  assert.equal((task as TaskContract).objective, "Ship the contracts migration");
  assert.deepEqual((task as TaskContract).metadata.legacy, { custom_hint: "preserve under metadata" });

  const decision = parseLegacyContract("routing_decision", {
    provider: "anthropic",
    model: "claude-sonnet-4",
    tier: 1,
    reason: "protected route",
    alternatives: ["openai/gpt-4o"],
    request_id: "req-123",
  });
  const typedDecision = decision as RoutingContract;
  assert.equal(typedDecision.selected_route.runtime_id, "anthropic/claude-sonnet-4");
  assert.equal(typedDecision.policy_floor, "protected");
  assert.deepEqual(typedDecision.exclusions, [{ model: "openai/gpt-4o", reason: "legacy alternative" }]);
});

test("outcome details are redacted before they enter a validated event", () => {
  const outcome = parseContract("outcome_event", {
    event_type: "execution_finished",
    outcome: "success",
    occurred_at: "2026-07-16T12:00:00Z",
    details: { password: "secret", prompt: "private prompt", note: "safe" },
  });
  assert.deepEqual(outcome.details, { password: "[redacted]", prompt: "[redacted]", note: "safe" });
});
