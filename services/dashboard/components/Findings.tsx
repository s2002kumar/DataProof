import type { Investigation, Reproduction, ReproductionRequest, ToolCall } from "@/lib/types";
import { Sql } from "./Sql";

/** What produced a finding, in words. */
export function describeModel(model: string): string {
  if (model === "dataproof:risk-rules-v1") return "Static risk rules (no model)";
  if (model === "dataproof:rules-v1") return "Rule-based diagnosis (no model)";
  const [provider, name] = model.split(":", 2);
  return provider === "anthropic" ? `AI: ${name}` : model;
}

/**
 * AI and rule-based findings, before the migration ran and after the checks: advisory, cited, and
 * never a verdict (ADR-0002). ``evidenceHref`` links a citation to where its evidence is shown.
 */
export function Findings({
  investigations,
  empty = "No investigation ran.",
  evidenceHref,
}: {
  investigations: Investigation[];
  empty?: string;
  evidenceHref?: (evidenceId: string) => string | null;
}) {
  if (investigations.length === 0) return <p className="muted">{empty}</p>;
  return (
    <div className="findings">
      {investigations.map((investigation) => {
        const analysis = investigation.role === "CHANGE_ANALYST";
        const shown = investigation.status === "COMPLETED";
        return (
          <article key={investigation.investigation_id} className={`finding ${shown ? "" : "quiet"}`}>
            <header>
              <span className="badge advisory">Advisory</span>{" "}
              <strong>{describeModel(investigation.model)}</strong>
              <span className="muted">
                {" "}
                · {analysis ? "before the migration ran" : "after the checks"} · {investigation.status}
              </span>
            </header>
            {investigation.hypotheses.length > 0 && (
              <ol className="hypotheses">
                {investigation.hypotheses.map((hypothesis, index) => (
                  <li key={index} className="hypothesis">
                    <p>{hypothesis.statement}</p>
                    <p className="cites">
                      <span className={`pill confidence-${hypothesis.confidence.toLowerCase()}`}>
                        {hypothesis.confidence} confidence
                      </span>{" "}
                      Cites:{" "}
                      {hypothesis.cited_evidence_ids.map((id) => {
                        const call = investigation.tool_calls.find((made) => made.evidence_id === id);
                        const href = call !== undefined ? `#${toolAnchor(investigation, call)}` : (evidenceHref?.(id) ?? null);
                        return href !== null ? (
                          <a key={id} className="chip" href={href}>
                            <code>{id}</code>
                          </a>
                        ) : (
                          <code key={id} className="chip">
                            {id}{" "}
                          </code>
                        );
                      })}
                    </p>
                  </li>
                ))}
              </ol>
            )}
            {investigation.suggested_action !== null && (
              <p>
                <strong>{analysis ? "Edge cases to cover:" : "Suggested:"}</strong> {investigation.suggested_action}
              </p>
            )}
            {investigation.caveat !== null && <p className="muted">{investigation.caveat}</p>}
            {investigation.tool_calls.length > 0 && <ToolCalls investigation={investigation} />}
            {investigation.failure !== null &&
              (investigation.status === "NOT_AVAILABLE" ? (
                <p className="muted">Nothing to report: {investigation.failure}.</p>
              ) : (
                <p className="warning">Not shown: {investigation.failure}</p>
              ))}
          </article>
        );
      })}
      <p className="muted">Findings are advisory and cite their evidence. Only the deterministic checks decide the verdict.</p>
    </div>
  );
}

const toolAnchor = (investigation: Investigation, call: ToolCall) =>
  `${investigation.investigation_id}-tool-${call.sequence}`;

/** What an investigation read or asked, call by call: its own evidence, and what was refused. */
function ToolCalls({ investigation }: { investigation: Investigation }) {
  return (
    <details className="tool-calls">
      <summary>What it read: {investigation.tool_calls.length} tool call{investigation.tool_calls.length === 1 ? "" : "s"}</summary>
      <ol>
        {investigation.tool_calls.map((call) => {
          const { sql, ...rest } = call.arguments;
          return (
            <li key={call.sequence} id={toolAnchor(investigation, call)}>
              <code>{call.tool}</code> <span className={`pill outcome-${call.outcome.toLowerCase()}`}>{call.outcome}</span>
              {call.evidence_id !== null && (
                <span className="muted">
                  {" "}
                  as <code>{call.evidence_id}</code>
                </span>
              )}
              {Object.keys(rest).length > 0 && <p className="muted">{JSON.stringify(rest)}</p>}
              {typeof sql === "string" && <Sql sql={sql} />}
              {call.detail !== null && <p className="warning">{call.detail}</p>}
            </li>
          );
        })}
      </ol>
    </details>
  );
}

export function Reproductions({
  reproductions,
  requests = [],
}: {
  reproductions: Reproduction[];
  requests?: ReproductionRequest[];
}) {
  const pending = requests.filter((request) => request.state !== "reproduced");
  if (reproductions.length === 0 && pending.length === 0) {
    return <p className="muted">No reproduction was attempted.</p>;
  }
  return (
    <ul>
      {pending.map((request) => (
        <li key={request.request_id}>
          <code>{request.check_id}</code>: {request.state}, requested by {request.requested_by}
          {request.failure !== null && <span className="warning"> ({request.failure})</span>}
        </li>
      ))}
      {reproductions.map((reproduction) => (
        <li key={reproduction.reproduction_id}>
          <code>{reproduction.check_id}</code>: <strong>{reproduction.status}</strong>
          {reproduction.reason !== null && <> ({reproduction.reason})</>}
          {reproduction.subset.length > 0 && (
            <span className="muted">
              {" "}
              on {reproduction.subset.reduce((rows, table) => rows + table.rows, 0)} rows from{" "}
              {reproduction.subset.map((table) => table.table).join(", ")}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
