import { describe, it, expect } from 'vitest';
import { VerdictClient } from "../src/index.js";
import { ContractValidationError } from "@bodanglin/verdict-contracts";

describe("VerdictClient", () => {
  it("should create a client with default options", () => {
    const client = new VerdictClient();
    expect(client).toBeInstanceOf(VerdictClient);
  });

  it("should create a client with custom baseUrl", () => {
    const client = new VerdictClient({ baseUrl: "http://localhost:8000/v1" });
    expect(client).toBeInstanceOf(VerdictClient);
  });

  it("should create a client with bearer token", () => {
    const client = new VerdictClient({ bearerToken: "test-token" });
    expect(client).toBeInstanceOf(VerdictClient);
  });

  it("should create a client with custom timeout", () => {
    const client = new VerdictClient({ timeoutMs: 5000 });
    expect(client).toBeInstanceOf(VerdictClient);
  });
});

describe("ContractValidationError", () => {
  it("should be importable from @bodanglin/verdict-contracts", () => {
    expect(ContractValidationError).toBeDefined();
  });
});
