// The control plane API, on the dashboard's own origin. Every write carries the session's CSRF
// token; the browser adds the Origin header the API checks.

import type { CheckRecord, HumanDecision, Role, RunDetail, RunSummary, Session } from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...init.headers },
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Not JSON: keep the status text.
    }
    throw new ApiError(response.status, message);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

function post<T>(path: string, csrf: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body),
  });
}

const run = (id: string) => `/api/runs/${encodeURIComponent(id)}`;

export interface DecisionResult {
  decision: HumanDecision;
  recorded: boolean;
  started_run_id: string | null;
}

export const api = {
  session: () => request<Session>("/api/session"),
  runs: (pullRequest?: number) =>
    request<RunSummary[]>(pullRequest ? `/api/runs?pull_request=${pullRequest}` : "/api/runs"),
  run: (id: string) => request<RunDetail>(run(id)),
  checks: (id: string) => request<CheckRecord[]>(`${run(id)}/checks`),
  decideBinding: (
    id: string,
    csrf: string,
    body: { action: "APPROVE_BINDING" | "REJECT_BINDING"; binding_digest: string; comment?: string },
  ) => post<DecisionResult>(`${run(id)}/binding-decisions`, csrf, body),
  cancel: (id: string, csrf: string, comment?: string) =>
    post<{ decision: HumanDecision }>(`${run(id)}/cancel`, csrf, { comment }),
  correction: (id: string, csrf: string, headSha: string, comment?: string) =>
    post<{ run_id: string; created: boolean }>(`${run(id)}/correction-replay`, csrf, {
      head_sha: headSha,
      comment,
    }),
  reproduce: (id: string, csrf: string, checkId: string) =>
    post<{ request: { request_id: string; state: string }; created: boolean }>(`${run(id)}/reproductions`, csrf, {
      check_id: checkId,
    }),
  previewLink: (id: string, csrf: string, role: Role) =>
    post<{ url: string; expires_at: string }>(`${run(id)}/previews/${role}`, csrf, {}),
  logout: (csrf: string) => post<void>("/auth/logout", csrf, {}),
};
