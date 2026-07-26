import type {
  TaskSpec,
  RoutingDecision,
  AvailabilitySnapshot,
  RuntimeCandidate,
} from '@bodanglin/verdict-contracts';
import {
  parseContract,
  ContractValidationError as ContractsContractValidationError,
} from '@bodanglin/verdict-contracts';
import {
  VerdictAPIError,
  BadRequestError,
  UnauthorizedError,
  ForbiddenError,
  NotFoundError,
  InternalServerError,
  BadGatewayError,
  ServiceUnavailableError,
  NetworkError,
  TimeoutError,
  ContractValidationError,
  isVerdictAPIError,
  createVerdictAPIError,
} from './errors.js';

/**
 * Configuration options for VerdictClient
 */
export interface VerdictClientOptions {
  /** Base URL of the Verdict API (e.g., 'http://localhost:8000/v1') */
  baseUrl?: string;
  /** Bearer token for authentication */
  bearerToken?: string | undefined;
  /** Default timeout for requests in milliseconds */
  timeoutMs?: number;
  /** Custom fetch implementation (for testing or non-browser environments) */
  fetchImpl?: typeof fetch;
}

/**
 * Parameters for the explain endpoint
 */
export interface ExplainParams {
  /** Specific model to get availability for */
  model_id?: string;
  /** Evidence ID to look up */
  evidence_id?: string;
  /** Request ID to look up */
  request_id?: string;
  /** Correlation ID to look up */
  correlation_id?: string;
}

/**
 * Cache summary response from /v1/route/explain (no model_id)
 */
export interface AvailabilityCacheSummary {
  kind: 'availability_explain';
  policy_version: string;
  cached_models: string[];
  cache_state: 'configured';
  eligible_set: string[];
  exclusions: Array<{
    model: string;
    state: string;
    rejected: boolean;
    reason: string;
  }>;
}

/**
 * Model-specific explain response from /v1/route/explain?model_id=...
 */
export interface ModelAvailabilityExplain {
  kind: 'availability_explain';
  observed_at: string;
  state: string;
  signals: Record<string, unknown>;
  candidates: RuntimeCandidate[];
  source: string;
  ttl_seconds: number;
  expires_at: string | null;
  eligibility?: Record<string, unknown>;
  eligible?: boolean;
}

/**
 * Union type for explain endpoint responses
 */
export type ExplainResponse = AvailabilityCacheSummary | ModelAvailabilityExplain;

/**
 * Response from /v1/models endpoint
 */
export interface ModelsResponse {
  data: Array<{
    id: string;
    provider: string;
    capability_tier: number;
    context_window: number | null;
    capabilities: string[];
    is_available: boolean;
    availability_state: string;
    source: string;
  }>;
}

/**
 * Health check response
 */
export interface HealthResponse {
  status: string;
  engine: string;
}

/**
 * Readiness check response
 */
export interface ReadyResponse {
  ready: boolean;
  upstream?: {
    reachable: boolean;
    latency_ms: number;
  };
}

/**
 * Chat completion request (OpenAI-compatible)
 */
export interface ChatCompletionRequest {
  model: string;
  messages: Array<{
    role: 'system' | 'user' | 'assistant' | 'tool';
    content: string | null;
    name?: string;
    tool_call_id?: string;
    tool_calls?: unknown[];
  }>;
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
  max_completion_tokens?: number;
  n?: number;
  stop?: string | string[];
  presence_penalty?: number;
  frequency_penalty?: number;
  logit_bias?: Record<string, number>;
  logprobs?: boolean;
  top_logprobs?: number;
  seed?: number;
  tools?: unknown[];
  tool_choice?: unknown;
  parallel_tool_calls?: boolean;
  response_format?: { type: 'text' | 'json_object' | 'json_schema'; json_schema?: unknown };
  stream?: boolean;
  stream_options?: { include_usage?: boolean };
  metadata?: Record<string, string | number | boolean | null>;
  user?: string;
}

/**
 * Chat completion response (OpenAI-compatible)
 */
export interface ChatCompletionResponse {
  id: string;
  object: 'chat.completion';
  created: number;
  model: string;
  choices: Array<{
    index: number;
    message: {
      role: 'assistant';
      content: string | null;
      tool_calls?: unknown[];
      refusal?: string | null;
    };
    logprobs?: unknown;
    finish_reason: 'stop' | 'length' | 'tool_calls' | 'content_filter' | 'function_call' | null;
  }>;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    prompt_tokens_details?: Record<string, number>;
    completion_tokens_details?: Record<string, number>;
  };
  system_fingerprint?: string;
  service_tier?: string;
}

/**
 * Chat completion chunk for streaming
 */
export interface ChatCompletionChunk {
  id: string;
  object: 'chat.completion.chunk';
  created: number;
  model: string;
  choices: Array<{
    index: number;
    delta: {
      role?: 'assistant';
      content?: string | null;
      tool_calls?: unknown[];
      refusal?: string | null;
    };
    logprobs?: unknown;
    finish_reason: 'stop' | 'length' | 'tool_calls' | 'content_filter' | 'function_call' | null;
  }>;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  } | null;
  system_fingerprint?: string;
  service_tier?: string;
}

/**
 * Main Verdict client class
 */
export class VerdictClient {
  private readonly baseUrl: string;
  private readonly bearerToken?: string | undefined;
  private readonly defaultTimeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: VerdictClientOptions = {}) {
    const baseUrl = options.baseUrl ?? 'http://localhost:8000/v1';
    this.baseUrl = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
    this.bearerToken = options.bearerToken;
    this.defaultTimeoutMs = options.timeoutMs ?? 30000;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  /**
   * Build headers for requests
   */
  private buildHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    };
    if (this.bearerToken) {
      headers.Authorization = `Bearer ${this.bearerToken}`;
    }
    return headers;
  }

  /**
   * Execute a fetch request with timeout and error handling
   */
  private async request<T>(
    path: string,
    init: RequestInit = {},
    timeoutMs?: number
  ): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs ?? this.defaultTimeoutMs);

    const url = `${this.baseUrl}${path}`;
    const headers = this.buildHeaders();

    try {
      const response = await this.fetchImpl(url, {
        ...init,
        headers: { ...headers, ...init.headers },
        signal: (init.signal ?? controller.signal) as AbortSignal | null,
      });

      clearTimeout(timeout);

      if (!response.ok) {
        let body: unknown;
        try {
          body = await response.json();
        } catch {
          body = await response.text();
        }

        throw createVerdictAPIError(response.status, body);
      }

      // Handle 204 No Content
      if (response.status === 204) {
        return undefined as T;
      }

      const data = await response.json();
      return data as T;
    } catch (error) {
      clearTimeout(timeout);

      if (error instanceof VerdictAPIError) {
        throw error;
      }

      if (error instanceof DOMException && error.name === 'AbortError') {
        throw new TimeoutError(`Request to ${path} timed out after ${timeoutMs ?? this.defaultTimeoutMs}ms`);
      }

      if (error instanceof TypeError && error.message.includes('fetch')) {
        throw new NetworkError(error as Error, `Network error calling ${path}: ${error.message}`);
      }

      throw error;
    }
  }

  /**
   * Route a task to a model
   * POST /v1/route
   */
  async route(
    taskSpec: TaskSpec,
    options: { signal?: AbortSignal; timeoutMs?: number } = {}
  ): Promise<RoutingDecision> {
    const response = await this.request<unknown>(
      '/route',
      {
        method: 'POST',
        body: JSON.stringify(taskSpec),
        signal: options.signal ?? null,
      },
      options.timeoutMs
    );

    // The Python API returns a legacy decision dict, not the canonical RoutingDecisionContract.
    // We need to map it to the canonical contract shape.
    return this.mapLegacyDecisionToContract(response);
  }

  /**
   * Get availability explanation or cache summary
   * GET /v1/route/explain
   */
  async explain(
    params: ExplainParams = {},
    options: { signal?: AbortSignal; timeoutMs?: number } = {}
  ): Promise<ExplainResponse> {
    const searchParams = new URLSearchParams();
    if (params.model_id) searchParams.set('model_id', params.model_id);
    if (params.evidence_id) searchParams.set('evidence_id', params.evidence_id);
    if (params.request_id) searchParams.set('request_id', params.request_id);
    if (params.correlation_id) searchParams.set('correlation_id', params.correlation_id);

    const query = searchParams.toString();
    const path = `/route/explain${query ? `?${query}` : ''}`;

    const response = await this.request<unknown>(
      path,
      { method: 'GET', signal: options.signal ?? null },
      options.timeoutMs
    );

    return response as ExplainResponse;
  }

  /**
   * Get list of available models
   * GET /v1/models
   */
  async models(
    options: { signal?: AbortSignal; timeoutMs?: number } = {}
  ): Promise<ModelsResponse> {
    return this.request<ModelsResponse>(
      '/models',
      { method: 'GET', signal: options.signal ?? null },
      options.timeoutMs
    );
  }

  /**
   * Health check endpoint
   * GET /health
   */
  async health(
    options: { signal?: AbortSignal; timeoutMs?: number } = {}
  ): Promise<HealthResponse> {
    return this.request<HealthResponse>(
      '/health',
      { method: 'GET', signal: options.signal ?? null },
      options.timeoutMs
    );
  }

  /**
   * Readiness check endpoint
   * GET /ready
   */
  async ready(
    options: { signal?: AbortSignal; timeoutMs?: number } = {}
  ): Promise<ReadyResponse> {
    return this.request<ReadyResponse>(
      '/ready',
      { method: 'GET', signal: options.signal ?? null },
      options.timeoutMs
    );
  }

  /**
   * Proxy a chat completion request through Verdict routing
   * POST /v1/chat/completions
   */
  async chatCompletions(
    request: ChatCompletionRequest,
    options: { signal?: AbortSignal; timeoutMs?: number } = {}
  ): Promise<ChatCompletionResponse> {
    return this.request<ChatCompletionResponse>(
      '/chat/completions',
      {
        method: 'POST',
        body: JSON.stringify(request),
        signal: options.signal ?? null,
      },
      options.timeoutMs
    );
  }

  /**
   * Stream a chat completion request
   * POST /v1/chat/completions (with stream: true)
   */
  async *chatCompletionsStream(
    request: ChatCompletionRequest,
    options: { signal?: AbortSignal; timeoutMs?: number } = {}
  ): AsyncIterable<ChatCompletionChunk> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? this.defaultTimeoutMs);

    const combinedSignal = options.signal
      ? AbortSignal.any([options.signal, controller.signal])
      : controller.signal;

    try {
      const response = await this.fetchImpl(`${this.baseUrl}/chat/completions`, {
        method: 'POST',
        headers: this.buildHeaders(),
        body: JSON.stringify({ ...request, stream: true }),
        signal: combinedSignal,
      });

      clearTimeout(timeout);

      if (!response.ok) {
        let body: unknown;
        try {
          body = await response.json();
        } catch {
          body = await response.text();
        }
        throw createVerdictAPIError(response.status, body);
      }

      if (!response.body) {
        throw new NetworkError(new Error('No response body'), 'Empty response body for stream');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          let boundary = buffer.indexOf('\n\n');
          while (boundary !== -1) {
            const eventText = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);

            const lines = eventText
              .split(/\r?\n/)
              .filter((line) => line.startsWith('data:'))
              .map((line) => line.slice(5).trimStart());

            if (lines.length > 0) {
              const data = lines.join('\n').trim();
              if (data === '[DONE]') return;

              try {
                const chunk = JSON.parse(data) as ChatCompletionChunk;
                yield chunk;
              } catch {
                // Ignore malformed chunks in stream
              }
            }

            boundary = buffer.indexOf('\n\n');
          }
        }
      } finally {
        reader.releaseLock();
      }
    } catch (error) {
      clearTimeout(timeout);
      if (error instanceof VerdictAPIError) throw error;
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw new TimeoutError('Stream request timed out');
      }
      if (error instanceof TypeError && error.message.includes('fetch')) {
        throw new NetworkError(error as Error, `Network error in stream: ${error.message}`);
      }
      throw error;
    }
  }

  /**
   * Map legacy Python API decision response to canonical RoutingDecision contract
   */
  private mapLegacyDecisionToContract(legacy: unknown): RoutingDecision {
    if (!legacy || typeof legacy !== 'object') {
      throw new ContractValidationError('routing_decision', [], 'Invalid decision response: not an object');
    }

    const decision = legacy as Record<string, unknown>;

    // Build the canonical RoutingDecision shape
    const canonical: Record<string, unknown> = {
      selected_route: decision.selected_route ?? {},
      task_spec: decision.task_spec ?? {},
      candidate_snapshot: decision.candidate_snapshot ?? null,
      exclusions: Array.isArray(decision.exclusions) ? decision.exclusions : [],
      policy_floor: decision.policy_floor ?? 'none',
      planner_mode: decision.planner_mode ?? 'default',
      explanation: typeof decision.explanation === 'string' ? decision.explanation : '',
      adaptive_influence: decision.adaptive_influence ?? {},
      fallback_plan: Array.isArray(decision.fallback_plan) ? decision.fallback_plan : [],
      correlation_id: decision.correlation_id ?? null,
      request_id: decision.request_id ?? null,
      policy_version: String(decision.policy_version ?? '1'),
      schema_version: '1',
    };

    try {
      return parseContract('routing_decision', canonical);
    } catch (error) {
      if (error instanceof ContractsContractValidationError) {
        throw new ContractValidationError(
          'routing_decision',
          error.path,
          `Failed to validate routing decision: ${error.message}`
        );
      }
      throw error;
    }
  }
}

/**
 * Default client instance for convenience
 */
export const defaultVerdictClient = new VerdictClient();

/**
 * Helper to create a client with custom options
 */
export function createVerdictClient(options: VerdictClientOptions): VerdictClient {
  return new VerdictClient(options);
}

/**
 * Map HTTP status to error class
 */
function statusToErrorClass(status: number): typeof VerdictAPIError {
  switch (status) {
    case 400: return BadRequestError;
    case 401: return UnauthorizedError;
    case 403: return ForbiddenError;
    case 404: return NotFoundError;
    case 500: return InternalServerError;
    case 502: return BadGatewayError;
    case 503: return ServiceUnavailableError;
    default:
      if (status >= 400 && status < 500) return BadRequestError;
      if (status >= 500) return InternalServerError;
      return VerdictAPIError;
  }
}