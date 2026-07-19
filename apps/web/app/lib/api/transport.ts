const defaultApiBaseUrl = "http://localhost:8000";

function configuredBaseUrl() {
  const value = process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || defaultApiBaseUrl;
  return value.replace(/\/+$/, "");
}

export function apiBaseUrl() {
  return configuredBaseUrl();
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${configuredBaseUrl()}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });

  const contentType = response.headers.get("content-type") ?? "";
  const body: unknown = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String(body.detail)
        : `요청에 실패했습니다. (HTTP ${response.status})`;
    throw new ApiError(detail, response.status);
  }

  return body as T;
}
