import { formatKey, formatValue } from "@/lib/format";
import type { CheckRecord } from "@/lib/types";
import { BindingReview, type BindingReviewProps } from "./BindingReview";

export function Checks({
  checks,
  onDecide,
  onReproduce,
}: {
  checks: CheckRecord[];
  onDecide: (digest: string, ...decision: Parameters<BindingReviewProps["onDecide"]>) => Promise<void>;
  /** Offered for failed data checks; API behaviour checks need live previews. */
  onReproduce?: (checkId: string) => void;
}) {
  return (
    <div className="checks">
      {checks.map(({ result, binding, binding_digest, binding_analysis, binding_problem }) => (
        <article key={result.check_id} className={`check ${result.outcome.toLowerCase()}`}>
          <h3>
            <code>{result.check_id}</code> <span className="outcome">{result.outcome}</span>
            {result.required ? null : <span className="muted"> (optional)</span>}
          </h3>
          {(result.not_run_reason ?? result.error_code) !== null && (
            <p>Reason: {result.not_run_reason ?? result.error_code}</p>
          )}
          {result.expected_key_count !== null && (
            <p className="muted">
              {result.expected_key_count} expected keys, {result.observed_key_count ?? 0} observed,{" "}
              {result.mismatch_count} mismatches
            </p>
          )}
          {result.mismatch_examples.length > 0 && (
            <table>
              <caption>
                First {result.mismatch_examples.length} of {result.mismatch_count} mismatches
              </caption>
              <thead>
                <tr>
                  <th>Key</th>
                  <th>Column</th>
                  <th>Expected (reference)</th>
                  <th>Observed (candidate)</th>
                </tr>
              </thead>
              <tbody>
                {result.mismatch_examples.map((mismatch, index) => (
                  <tr key={index}>
                    <td>{formatKey(mismatch.key)}</td>
                    <td>{mismatch.column ?? "(row)"}</td>
                    <td>{formatValue(mismatch.expected)}</td>
                    <td>{formatValue(mismatch.observed)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {onReproduce !== undefined && result.outcome === "FAIL" && binding?.api_route == null && (
            <button type="button" className="secondary" onClick={() => onReproduce(result.check_id)}>
              Reproduce on a small subset
            </button>
          )}
          {result.evidence_ids.length > 0 && (
            <p className="muted">
              Evidence:{" "}
              {result.evidence_ids.map((id) => (
                <code key={id}>{id} </code>
              ))}
            </p>
          )}
          {result.not_run_reason === "AWAITING_BINDING_REVIEW" && binding !== null && binding_digest !== null && (
            <BindingReview
              checkId={result.check_id}
              binding={binding}
              digest={binding_digest}
              analysis={binding_analysis}
              problem={binding_problem}
              onDecide={(action, comment) => onDecide(binding_digest, action, comment)}
            />
          )}
          {result.not_run_reason === "AWAITING_BINDING_REVIEW" && binding === null && binding_problem !== null && (
            <p className="warning">{binding_problem}</p>
          )}
        </article>
      ))}
    </div>
  );
}
