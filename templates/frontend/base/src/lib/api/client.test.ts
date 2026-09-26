import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("apiRequest", () => {
  it("parses the backend error envelope and request ID", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: "validation_error", message: "Invalid", request_id: "req-1", details: [] },
    }), { status: 422, headers: { "Content-Type": "application/json", "X-Request-ID": "req-1" } })));
    await expect(apiRequest("/bad", { authenticate: false })).rejects.toMatchObject<ApiError>({
      status: 422,
      error: { code: "validation_error", request_id: "req-1" },
    });
  });

  it("always sends credentialed requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ok" }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    await apiRequest("/health/live", { authenticate: false });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });
  });
});
