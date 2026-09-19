export class BridgeError extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.name = 'BridgeError';
    this.code = code;
  }
}

/** A route failure with its HTTP status. The body carries only the stable code. */
export class ApiError extends BridgeError {
  readonly status: number;
  constructor(status: number, code: string) {
    super(code);
    this.name = 'ApiError';
    this.status = status;
  }
}
