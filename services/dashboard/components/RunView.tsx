"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { formatTime, shortSha } from "@/lib/format";
import type { CheckRecord, Role, RunDetail, Session } from "@/lib/types";
import { Checks } from "./Checks";
import { Findings, Reproductions } from "./Findings";
import { SignIn } from "./SignIn";
import { VerdictBadge } from "./VerdictBadge";

const SHA = /^[0-9a-f]{40}$/;

export function RunView({ id }: { id: string }) {
  const [session, setSession] = useState<Session | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [checks, setChecks] = useState<CheckRecord[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [correction, setCorrection] = useState("");

  const load = useCallback(async () => {
    try {
      const current = await api.session();
      setSession(current);
      const [detail, records] = await Promise.all([api.run(id), api.checks(id)]);
      setRun(detail);
      setChecks(records);
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, String(caught)));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error?.status === 401) return <SignIn />;
  if (error !== null) return <p className="error">{error.status === 404 ? "No such run." : error.message}</p>;
  if (session === null || run === null) return <p className="muted">Loading…</p>;

  const csrf = session.csrf_token;
  const inputs = run.run.inputs;

  async function act(action: () => Promise<string>) {
    setNotice(null);
    try {
      setNotice(await action());
      await load();
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function openPreview(role: Role) {
    const link = await api.previewLink(id, csrf, role);
    window.open(link.url, "_blank", "noopener,noreferrer");
  }

  return (
    <div>
      <p>
        <Link href="/">All runs</Link>
      </p>
      <h1>
        Run <code>{run.run.run_id.slice(0, 8)}</code> <VerdictBadge verdict={run.report?.verdict ?? null} />
      </h1>
      <dl className="facts">
        <dt>Repository</dt>
        <dd>
          {inputs.repository}
          {inputs.pull_request !== null && <> #{inputs.pull_request}</>}
          {run.pull_request_author !== null && <> by {run.pull_request_author}</>}
        </dd>
        <dt>Commits</dt>
        <dd>
          base <code>{shortSha(inputs.base_sha)}</code>, head <code>{shortSha(inputs.head_sha)}</code>
          {inputs.head_repository !== null && <> from fork {inputs.head_repository}</>}
        </dd>
        <dt>State</dt>
        <dd>
          {run.run.state}
          {run.run.error_code !== null && <> (ERROR {run.run.error_code})</>}
          {run.run.not_executed_reason !== null && <> ({run.run.not_executed_reason})</>}
          {run.run.superseded_by !== null && (
            <>
              , superseded by <Link href={`/runs/${run.run.superseded_by}`}>a newer run</Link>
            </>
          )}
        </dd>
        <dt>Trigger</dt>
        <dd>
          {run.run.trigger}
          {run.run.parent_run_id !== null && (
            <>
              {" "}
              from <Link href={`/runs/${run.run.parent_run_id}`}>{run.run.parent_run_id.slice(0, 8)}</Link>
            </>
          )}
        </dd>
        <dt>GitHub check</dt>
        <dd>
          {run.check_run !== null ? (
            <a href={run.check_run.url}>check run {run.check_run.id}</a>
          ) : run.check_run_omitted_for !== null ? (
            <>
              none: <Link href={`/runs/${run.check_run_omitted_for}`}>a newer run</Link> on this commit reports to
              GitHub
            </>
          ) : (
            "not published"
          )}
        </dd>
      </dl>

      {notice !== null && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}

      <section>
        <h2>Coverage</h2>
        {run.report === null ? (
          <p className="muted">Not reported yet.</p>
        ) : (run.report.report.coverage ?? []).length === 0 ? (
          <p className="muted">
            No checks ran
            {run.report.report.not_executed_reason !== undefined && <> ({run.report.report.not_executed_reason})</>}.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Required</th>
                <th>Outcome</th>
                <th>Reason</th>
                <th>Mismatches</th>
              </tr>
            </thead>
            <tbody>
              {(run.report.report.coverage ?? []).map((row) => (
                <tr key={row.check_id} className={row.outcome.toLowerCase()}>
                  <td>
                    <code>{row.check_id}</code>
                  </td>
                  <td>{row.required ? "yes" : "no"}</td>
                  <td>{row.outcome}</td>
                  <td>{row.reason ?? ""}</td>
                  <td>{row.mismatch_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section>
        <h2>Previews</h2>
        <div className="actions">
          {(["reference", "candidate"] as const).map((role) => (
            <button key={role} type="button" onClick={() => act(async () => (await openPreview(role), `Opened the ${role} preview.`))}>
              Open {role} preview
            </button>
          ))}
        </div>
        <p className="muted">Each preview opens on its own host, behind a one-hour link that only you can use.</p>
      </section>

      <section>
        <h2>Checks</h2>
        <Checks
          checks={checks}
          onReproduce={(checkId) =>
            void act(async () => {
              const queued = await api.reproduce(id, csrf, checkId);
              return queued.created ? "Reproduction queued; a worker will run it." : `Already requested (${queued.request.state}).`;
            })
          }
          onDecide={async (digest, action, comment) => {
            const result = await api.decideBinding(id, csrf, {
              action,
              binding_digest: digest,
              ...(comment ? { comment } : {}),
            });
            setNotice(
              result.started_run_id !== null
                ? `Approved. A new run started: ${result.started_run_id}.`
                : `${result.decision.action} recorded${result.recorded ? "" : " earlier"}.`,
            );
            await load();
          }}
        />
      </section>

      <section>
        <h2>Findings</h2>
        <Findings investigations={run.investigations} />
        <h3>Reproduction</h3>
        <Reproductions reproductions={run.reproductions} requests={run.reproduction_requests} />
      </section>

      <section>
        <h2>Decisions</h2>
        {run.decisions.length === 0 ? (
          <p className="muted">None yet.</p>
        ) : (
          <ul>
            {run.decisions.map((decision) => (
              <li key={decision.decision_id}>
                {formatTime(decision.decided_at)}: {decision.actor} {decision.action}
                {decision.binding_digest !== null && (
                  <>
                    {" "}
                    <code>{decision.binding_digest.slice(0, 12)}</code>
                  </>
                )}
                {decision.approver_separation !== null && <> ({decision.approver_separation})</>}
                {decision.comment !== null && <>: {decision.comment}</>}
              </li>
            ))}
          </ul>
        )}
        <form
          className="actions"
          onSubmit={(event) => {
            event.preventDefault();
            const sha = correction.trim();
            if (!SHA.test(sha)) {
              setNotice("A correction is a full 40-character commit SHA.");
              return;
            }
            void act(async () => `Correction run started: ${(await api.correction(id, csrf, sha)).run_id}.`);
          }}
        >
          <label>
            Correction commit
            <input value={correction} onChange={(event) => setCorrection(event.target.value)} placeholder="40-character SHA" />
          </label>
          <button type="submit">Replay a correction</button>
          <button
            type="button"
            className="secondary"
            onClick={() => {
              if (window.confirm("Cancel this run? Its environments are cleaned up.")) {
                void act(async () => (await api.cancel(id, csrf), "Cancelled."));
              }
            }}
          >
            Cancel run
          </button>
        </form>
      </section>

      <section>
        <h2>Timeline</h2>
        <ol className="timeline">
          {run.transitions.map((transition) => (
            <li key={transition.seq}>
              {formatTime(transition.at)}: {transition.from_state ?? "created"} → {transition.to_state}{" "}
              <span className="muted">by {transition.actor}</span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
