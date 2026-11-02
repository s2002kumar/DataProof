"use client";

import { useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { RunSummary, Session } from "@/lib/types";
import { RunList } from "./RunList";
import { SignIn } from "./SignIn";

export function Runs() {
  const [session, setSession] = useState<Session | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setSession(await api.session());
        setRuns(await api.runs());
      } catch (caught) {
        setError(caught instanceof ApiError ? caught : new ApiError(0, String(caught)));
      }
    })();
  }, []);

  if (error?.status === 401) return <SignIn />;
  if (error !== null) return <p className="error">{error.message}</p>;
  if (session === null || runs === null) return <p className="muted">Loading…</p>;
  return (
    <div>
      <p className="muted">
        Signed in as {session.login}.{" "}
        <button
          type="button"
          className="link"
          onClick={async () => {
            await api.logout(session.csrf_token);
            window.location.assign("/");
          }}
        >
          Sign out
        </button>
      </p>
      <RunList runs={runs} />
    </div>
  );
}
