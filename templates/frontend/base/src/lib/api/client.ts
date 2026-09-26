import { env } from "@/lib/env";

export type ApiErrorBody = {
  code: string;
  message: string;
  request_id: string;
  details: unknown;
};

export class ApiError extends Error {
  constructor(public status: number, public error: ApiErrorBody) {
    super(error.message);
    this.name = "ApiError";
  }
}

type Options = Omit<RequestInit, "body"> & {
  body?: BodyInit | Record<string, unknown> | null;
  authenticate?: boolean;
  retryAuth?: boolean;
};

let accessToken: string | null = null;
let refreshPromise: Promise<string | null> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function readCookie(name: string) {
  if (typeof document === "undefined") return null;
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : null;
}

async function parseError(response: Response) {
  const requestId = response.headers.get("X-Request-ID") ?? "unknown";
  try {
    const payload = (await response.json()) as { error?: Partial<ApiErrorBody> };
    if (payload.error) {
      return new ApiError(response.status, {
        code: payload.error.code ?? `http_${response.status}`,
        message: payload.error.message ?? response.statusText,
        request_id: payload.error.request_id ?? requestId,
        details: payload.error.details ?? null,
      });
    }
  } catch {}
  return new ApiError(response.status, {
    code: `http_${response.status}`,
    message: response.statusText || "Request failed",
    request_id: requestId,
    details: null,
  });
}

async function refreshAccessToken() {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      const csrf = readCookie("csrf_token");
      if (!csrf) return null;
      const response = await fetch(`${env.NEXT_PUBLIC_API_BASE_URL}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": csrf },
      });
      if (!response.ok) return null;
      const payload = (await response.json()) as { access_token: string };
      setAccessToken(payload.access_token);
      return payload.access_token;
    })().finally(() => { refreshPromise = null; });
  }
  return refreshPromise;
}

export async function apiRequest<T>(path: string, options: Options = {}): Promise<T> {
  const { body, authenticate = true, retryAuth = true, headers, ...init } = options;
  const requestHeaders = new Headers(headers);
  if (body && !(body instanceof FormData)) requestHeaders.set("Content-Type", "application/json");
  if (authenticate && accessToken) requestHeaders.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(new URL(path, env.NEXT_PUBLIC_API_BASE_URL), {
    ...init,
    credentials: "include",
    headers: requestHeaders,
    body: body && typeof body === "object" && !(body instanceof FormData) ? JSON.stringify(body) : body,
  });
  if (response.status === 401 && authenticate && retryAuth && await refreshAccessToken()) {
    return apiRequest<T>(path, { ...options, retryAuth: false });
  }
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return null as T;
  return await response.json() as T;
}

export async function signIn(email: string, password: string) {
  const result = await apiRequest<{ access_token: string; expires_in: number }>("/auth/login", {
    method: "POST", body: { email, password }, authenticate: false,
  });
  setAccessToken(result.access_token);
  return result;
}

export async function signOut() {
  const csrf = readCookie("csrf_token");
  try {
    await apiRequest<null>("/auth/logout", {
      method: "POST",
      headers: csrf ? { "X-CSRF-Token": csrf } : {},
      retryAuth: false,
    });
  } finally {
    setAccessToken(null);
  }
}
