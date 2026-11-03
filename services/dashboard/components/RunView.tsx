"use client";

import Link from "next/link";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { formatTime, shortSha } from "@/lib/format";
import { isAnalysis, journey, nextStep, type Step } from "@/lib/journey";
import type { CheckRecord, Role, RunDetail, Session } from "@/lib/types";
import { BindingReviews, Checks, checkAnchor } from "./Checks";
import { Findings, Reproductions } from "./Findings";
import { Journey } from "./Journey";
import { SignIn } from "./SignIn";
import { MEANINGS, VerdictBadge } from "./VerdictBadge";

const SHA = /^[0-9a-f]{40}$/;

function Stage({ step, intro, children }: { step: Step; intro: string; children: ReactNode }) {
  return (
    <section id={step.id} aria-labelledby={`${step.id}-title`} className={`stage ${step.tone}`}>
      <h2 id={`${step.id}-title`}>
        <span className="step-number" aria-hidden="true">
          {step.number}
        </span>{" "}
        {step.title}
      </h2>
      <p className="stage-status">
        <span className={`pill tone-${step.tone}`}>{step.status}</span>
      </p>
      <p className="intro">{intro}</p>
      {children}
    </section>
  );
}

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
  const steps = journey(run, checks);
  const next = nextStep(steps);
  const [risk, verification, evidence, diagnosis, decision] = steps;
  const owners = new Map(checks.flatMap(({ result }) => result.evidence_ids.map((e) => [e, result.check_id] as const)));
  const evidenceHref = (evidenceId: string) => {
    const owner = owners.get(evidenceId);
    return owner === undefined ? null : `#${checkAnchor(owner)}`;
  };

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

  const coverage = run.report?.report.coverage ?? [];
  return (
    <article className="run">
      <p className="crumbs">
        <Link href="/">All runs</Link>
      </p>
      <header className="run-header">
        <h1>
          Run <code>{run.run.run_id.slice(0, 8)}</code> <VerdictBadge verdict={run.report?.verdict ?? null} />
        </h1>
        <p className="verdict-meaning">
          {run.report === null ? "This run has not reported yet." : `${MEANINGS[run.report.verdict]}.`}
          {next !== null && (
            <>
              {" "}
              Next: <a href={`#${next.id}`}>{next.title.toLowerCase()}</a> ({next.status.toLowerCase()}).
            </>
          )}
        </p>
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
      </header>

      {notice !== null && (
        <div className="notice" role="status">
          <span>{notice}</span>
          <button type="button" className="link" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

      <Journey steps={steps} next={next} />

      <Stage
        step={risk}
        intro="Before anything ran, the change was read for patterns that often go wrong in data migrations. These are reasons to look, not faults: the checks decide."
      >
        <Findings investigations={run.investigations.filter(isAnalysis)} empty="This run was not analysed before it ran." />
      </Stage>

      <Stage
        step={verification}
        intro="Isolated copies of the database were migrated, and every protected contract was checked against them. Required checks decide the verdict."
      >
        {run.report === null ? (
          <p className="muted">Not reported yet.</p>
        ) : coverage.length === 0 ? (
          <p className="muted">
            No checks ran
            {run.report.report.not_executed_reason !== undefined && <> ({run.report.report.not_executed_reason})</>}.
          </p>
        ) : (
          <div className="table-scroll" role="region" aria-label="Coverage" tabIndex={0}>
            <table className="coverage">
              <caption>Coverage: every check the report recorded</caption>
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">Required</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Reason</th>
                  <th scope="col" className="num">
                    Mismatches
                  </th>
                </tr>
              </thead>
              <tbody>
                {coverage.map((row) => (
                  <tr key={row.check_id} className={row.outcome.toLowerCase()}>
                    <td>
                      <a href={`#${checkAnchor(row.check_id)}`}>
                        <code>{row.check_id}</code>
                      </a>
                    </td>
                    <td>{row.required ? "yes" : "no"}</td>
                    <td>
                      <span className={`pill outcome-${row.outcome.toLowerCase()}`}>{row.outcome}</span>
                    </td>
                    <td>{row.reason ?? ""}</td>
                    <td className="num">{row.mismatch_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <h3>Previews</h3>
        <div className="actions">
          {(["reference", "candidate"] as const).map((role) => (
            <button key={role} type="button" onClick={() => act(async () => (await openPreview(role), `Opened the ${role} preview.`))}>
              Open {role} preview
            </button>
          ))}
        </div>
        <p className="muted">The base commit (reference) and the head commit after the migration (candidate), each on its own host, behind a one-hour link that only you can use.</p>
      </Stage>

      <Stage
        step={evidence}
        intro="What each check found, failures first: the first mismatches with the reference and candidate values, and the evidence each check recorded."
      >
        <Checks
          checks={checks}
          onReproduce={(checkId) =>
            void act(async () => {
              const queued = await api.reproduce(id, csrf, checkId);
              return queued.created ? "Reproduction queued; a worker will run it." : `Already requested (${queued.request.state}).`;
            })
          }
        />
        <h3>Reproduction</h3>
        <Reproductions reproductions={run.reproductions} requests={run.reproduction_requests} />
      </Stage>

      <Stage
        step={diagnosis}
        intro="Why the checks failed, from the recorded evidence. Advisory: every hypothesis cites what it rests on, and none can change a check or the verdict."
      >
        <Findings
          investigations={run.investigations.filter((investigation) => !isAnalysis(investigation))}
          empty={evidence.tone === "bad" ? "No diagnosis was recorded." : "Nothing failed, so there is nothing to diagnose."}
          evidenceHref={evidenceHref}
        />
      </Stage>

      <Stage
        step={decision}
        intro="What only a person decides: which bindings may read the candidate, whether to replay a correction, and, in the end, whether to merge."
      >
        <h3>Bindings awaiting review</h3>
        <BindingReviews
          checks={checks}
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
        <h3>Decisions</h3>
        {run.run.parent_run_id !== null && run.run.trigger !== "manual" && run.run.trigger !== "webhook" && (
          <p className="muted">
            The decision that started this run is recorded on{" "}
            <Link href={`/runs/${run.run.parent_run_id}#decision`}>run {run.run.parent_run_id.slice(0, 8)}</Link>.
          </p>
        )}
        {run.decisions.length === 0 ? (
          <p className="muted">None on this run yet.</p>
        ) : (
          <ul className="decisions">
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
        <h3>Correction</h3>
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
            <input
              value={correction}
              onChange={(event) => setCorrection(event.target.value)}
              placeholder="40-character SHA"
              spellCheck={false}
              autoComplete="off"
            />
          </label>
          <button type="submit" className="primary">
            Replay a correction
          </button>
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
        <p className="muted">A correction is evaluated as a new run on the same baseline. This run, its report and its evidence stay as they are.</p>
      </Stage>

      <section aria-labelledby="timeline-title">
        <h2 id="timeline-title">Timeline</h2>
        <details>
          <summary>{run.transitions.length} transitions, with who made each</summary>
          <ol className="timeline">
            {run.transitions.map((transition) => (
              <li key={transition.seq}>
                {formatTime(transition.at)}: {transition.from_state ?? "created"} → {transition.to_state}{" "}
                <span className="muted">by {transition.actor}</span>
              </li>
            ))}
          </ol>
        </details>
      </section>
    </article>
  );
}
