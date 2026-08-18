export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function requestJson<T>(
  input: RequestInfo | URL,
  init: RequestInit = {},
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(input, {
    cache: "no-store",
    ...init,
    signal,
    headers: { Accept: "application/json", ...init.headers },
  });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) throw new ApiError(`Request failed (${response.status})`, response.status);
    throw new ApiError("The server returned invalid JSON.", response.status);
  }
  if (!response.ok) {
    const errorPayload = payload as { error?: string | { message?: string } } | null;
    const message = typeof errorPayload?.error === "string"
      ? errorPayload.error
      : errorPayload?.error?.message || `Request failed (${response.status})`;
    throw new ApiError(message, response.status);
  }
  return payload as T;
}
