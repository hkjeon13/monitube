import type { AuthUser } from "./types";
import { asRecord, asText } from "./normalizers";
import { ApiError, request } from "./transport";

export async function getCurrentUser(): Promise<AuthUser | null> {
  try {
    const response = await request<unknown>("/v1/auth/me", { method: "GET" });
    const username = asText(asRecord(response)?.username);
    return username ? { username } : null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export async function login(username: string, password: string): Promise<AuthUser> {
  const response = await request<unknown>("/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
  const value = asText(asRecord(response)?.username);
  if (!value) throw new ApiError("로그인 응답을 해석할 수 없습니다.", 502);
  return { username: value };
}

export async function register(username: string, password: string): Promise<AuthUser> {
  const response = await request<unknown>("/v1/auth/register", { method: "POST", body: JSON.stringify({ username, password }) });
  const value = asText(asRecord(response)?.username);
  if (!value) throw new ApiError("회원 생성 응답을 해석할 수 없습니다.", 502);
  return { username: value };
}
