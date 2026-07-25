/**
 * Verdict API error classes
 */
export type VerdictAPIErrorConstructor = new (...args: unknown[]) => VerdictAPIError;

export class VerdictAPIError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message: string
  ) {
    super(message);
    this.name = 'VerdictAPIError';
  }
}

export class BadRequestError extends VerdictAPIError {
  constructor(body: unknown) {
    super(400, body, 'Bad Request: Invalid request parameters');
    this.name = 'BadRequestError';
  }
}

export class UnauthorizedError extends VerdictAPIError {
  constructor(body: unknown) {
    super(401, body, 'Unauthorized: Authentication required');
    this.name = 'UnauthorizedError';
  }
}

export class ForbiddenError extends VerdictAPIError {
  constructor(body: unknown) {
    super(403, body, 'Forbidden: Access denied');
    this.name = 'ForbiddenError';
  }
}

export class NotFoundError extends VerdictAPIError {
  constructor(body: unknown) {
    super(404, body, 'Not Found: Resource does not exist');
    this.name = 'NotFoundError';
  }
}

export class InternalServerError extends VerdictAPIError {
  constructor(body: unknown) {
    super(500, body, 'Internal Server Error');
    this.name = 'InternalServerError';
  }
}

export class BadGatewayError extends VerdictAPIError {
  constructor(body: unknown) {
    super(502, body, 'Bad Gateway: Upstream service failed');
    this.name = 'BadGatewayError';
  }
}

export class ServiceUnavailableError extends VerdictAPIError {
  constructor(body: unknown) {
    super(503, body, 'Service Unavailable: Service temporarily unavailable');
    this.name = 'ServiceUnavailableError';
  }
}

export class NetworkError extends Error {
  constructor(
    public readonly cause: Error,
    message = 'Network request failed'
  ) {
    super(message);
    this.name = 'NetworkError';
  }
}

export class TimeoutError extends Error {
  constructor(message = 'Request timed out') {
    super(message);
    this.name = 'TimeoutError';
  }
}

export class ContractValidationError extends Error {
  constructor(
    public readonly contractName: string,
    public readonly issues: unknown,
    message: string
  ) {
    super(message);
    this.name = 'ContractValidationError';
  }
}

/**
 * Type guard to check if an error is a VerdictAPIError
 */
export function isVerdictAPIError(error: unknown): error is VerdictAPIError {
  return error instanceof VerdictAPIError;
}

/**
 * Map HTTP status code to appropriate error class
 */
export function createVerdictAPIError(status: number, body: unknown): VerdictAPIError {
  switch (status) {
    case 400: return new BadRequestError(body);
    case 401: return new UnauthorizedError(body);
    case 403: return new ForbiddenError(body);
    case 404: return new NotFoundError(body);
    case 500: return new InternalServerError(body);
    case 502: return new BadGatewayError(body);
    case 503: return new ServiceUnavailableError(body);
    default:
      if (status >= 400 && status < 500) return new BadRequestError(body);
      if (status >= 500) return new InternalServerError(body);
      return new VerdictAPIError(status, body, 'Unknown error');
  }
}