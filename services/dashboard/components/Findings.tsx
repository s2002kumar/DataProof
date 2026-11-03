import type { Investigation, Reproduction, ReproductionRequest } from "@/lib/types";

/** AI and rule-based findings, before the migration ran and after the checks: advisory, cited, and
 * never a verdict (ADR-0002). */
export function Findings({ investigations }: { investigations: Investigation[] }) {
  if (investigations.length === 0) return <p className="muted">No investigation ran.</p>;
  return (
    <div>
      {investigations.map((investigation) => (
        <article key={investigation.investigation_id} className="finding">
          <h3>
            <span className="badge advisory">Advisory</span>{" "}
            {investigation.role === "CHANGE_ANALYST" ? "Change analysis, before the migration ran" : "Failure investigation, after the checks"}
            : {investigation.model} <span className="muted">({investigation.status})</span>
          </h3>
          {investigation.hypotheses.map((hypothesis, index) => (
            <div key={index} className="hypothesis">
              <p>{hypothesis.statement}</p>
              <p className="muted">
                Confidence {hypothesis.confidence}. Cites:{" "}
                {hypothesis.cited_evidence_ids.map((id) => (
                  <code key={id}>{id} </code>
                ))}
              </p>
            </div>
          ))}
          {investigation.suggested_action !== null && <p>Suggested: {investigation.suggested_action}</p>}
          {investigation.caveat !== null && <p className="muted">{investigation.caveat}</p>}
          {investigation.failure !== null && <p className="warning">Did not complete: {investigation.failure}</p>}
        </article>
      ))}
      <p className="muted">Findings are advisory and cite their evidence. Only the deterministic checks decide the verdict.</p>
    </div>
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
