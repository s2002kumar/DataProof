import { afterEach, expect, it, vi } from "vitest";

import { ApiError, api } from "../lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(status: number, body?: unknown) {
  const fetchMock = vi.fn(
    async (_path: string, _init: RequestInit) =>
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

it("writes carry the session's CSRF token and only same-origin credentials", async () => {
  const fetchMock = stubFetch(201, { decision: {}, recorded: true, started_run_id: "r2" });
  const digest = "d".repeat(64);
  await api.decideBinding("run-1", "csrf-1", { action: "APPROVE_BINDING", binding_digest: digest });
  const [path, init] = fetchMock.mock.calls[0];
  expect(path).toBe("/api/runs/run-1/binding-decisions");
  expect(init.method).toBe("POST");
  expect(init.credentials).toBe("same-origin");
  expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-1");
  expect(JSON.parse(String(init.body))).toEqual({ action: "APPROVE_BINDING", binding_digest: digest });
});

it("a refusal becomes an ApiError carrying the server's reason", async () => {
  stubFetch(403, { detail: "the pull request's author cannot approve its bindings" });
  await expect(api.cancel("run-1", "csrf-1")).rejects.toEqual(
    new ApiError(403, "the pull request's author cannot approve its bindings"),
  );
});

it("run IDs are encoded, so they cannot change the route", async () => {
  const fetchMock = stubFetch(200, {});
  await api.run("../auth/logout");
  expect(fetchMock.mock.calls[0][0]).toBe("/api/runs/..%2Fauth%2Flogout");
});
