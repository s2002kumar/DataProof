import { formatKey, formatValue } from "@/lib/format";
import { awaitingReview } from "@/lib/journey";
import type { CheckRecord, CheckResult } from "@/lib/types";
import { BindingReview, type BindingReviewProps } from "./BindingReview";

export const checkAnchor = (checkId: string) => `check-${checkId}`;

const ORDER: Record<CheckResult["outcome"], number> = { FAIL: 0, ERROR: 1, NOT_RUN: 2, PASS: 3 };

/** One check's evidence: why it did not pass, its first mismatches, and what it cites. */
function CheckCard({ result, onReproduce, reproducible }: { result: CheckResult; onReproduce?: (checkId: string) => void; reproducible: boolean }) {
  const reason = result.not_run_reason ?? result.error_code;
  return (
    <article id={checkAnchor(result.check_id)} className={`check ${result.outcome.toLowerCase()}`}>
      <h3>
        <code>{result.check_id}</code> <span className={`pill outcome-${result.outcome.toLowerCase()}`}>{result.outcome}</span>
        {result.required ? null : <span className="muted"> (optional)</span>}
      </h3>
      {reason !== null && (
        <p>
          Reason: <code>{reason}</code>
          {result.not_run_reason === "AWAITING_BINDING_REVIEW" && (
            <>
              {" "}
              (<a href="#decision">review its binding in step 5</a>)
            </>
          )}
        </p>
      )}
      {result.expected_key_count !== null && (
        <p className="muted">
          {result.expected_key_count} expected keys, {result.observed_key_count ?? 0} observed, {result.mismatch_count} mismatches
        </p>
      )}
      {result.mismatch_examples.length > 0 && (
        <div className="table-scroll" role="region" aria-label={`Mismatches of ${result.check_id}`} tabIndex={0}>
          <table className="mismatches">
            <caption>
              First {result.mismatch_examples.length} of {result.mismatch_count} mismatches
            </caption>
            <thead>
              <tr>
                <th scope="col">Key</th>
                <th scope="col">Column</th>
                <th scope="col">Expected (reference)</th>
                <th scope="col">Observed (candidate)</th>
              </tr>
            </thead>
            <tbody>
              {result.mismatch_examples.map((mismatch, index) => (
                <tr key={index}>
                  <td>{formatKey(mismatch.key)}</td>
                  <td>{mismatch.column ?? "(row)"}</td>
                  <td className="value expected">{formatValue(mismatch.expected)}</td>
                  <td className="value observed">{formatValue(mismatch.observed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {onReproduce !== undefined && reproducible && (
        <button type="button" className="secondary" onClick={() => onReproduce(result.check_id)}>
          Reproduce on a small subset
        </button>
      )}
      {result.evidence_ids.length > 0 && (
        <p className="muted evidence">
          Evidence:{" "}
          {result.evidence_ids.map((id) => (
            <code key={id} className="chip">
              {id}{" "}
            </code>
          ))}
        </p>
      )}
    </article>
  );
}

/**
 * The checks as evidence: failed and errored first, then those that did not run, with passing
 * checks folded away. Binding reviews are not here: they are the reviewer's decision (step 5).
 */
export function Checks({ checks, onReproduce }: { checks: CheckRecord[]; onReproduce?: (checkId: string) => void }) {
  if (checks.length === 0) return <p className="muted">No check has been recorded yet.</p>;
  const ordered = [...checks].sort((a, b) => ORDER[a.result.outcome] - ORDER[b.result.outcome]);
  const open = ordered.filter(({ result }) => result.outcome !== "PASS");
  const passed = ordered.filter(({ result }) => result.outcome === "PASS");
  const card = ({ result, binding }: CheckRecord) => (
    <CheckCard
      key={result.check_id}
      result={result}
      onReproduce={onReproduce}
      // Data checks only: API behaviour checks need live previews.
      reproducible={result.outcome === "FAIL" && binding?.api_route == null}
    />
  );
  return (
    <div className="checks">
      {open.map(card)}
      {passed.length > 0 && (
        <details className="passed">
          <summary>
            {passed.length} check{passed.length === 1 ? "" : "s"} passed
          </summary>
          {passed.map(card)}
        </details>
      )}
    </div>
  );
}

/** Every binding awaiting review, each with what it reads and the reviewer's decision. */
export function BindingReviews({
  checks,
  onDecide,
}: {
  checks: CheckRecord[];
  onDecide: (digest: string, ...decision: Parameters<BindingReviewProps["onDecide"]>) => Promise<void>;
}) {
  const awaiting = awaitingReview(checks);
  const broken = checks.filter(
    ({ result, binding, binding_problem }) =>
      result.not_run_reason === "AWAITING_BINDING_REVIEW" && binding === null && binding_problem !== null,
  );
  if (awaiting.length === 0 && broken.length === 0) return <p className="muted">No binding awaits review.</p>;
  return (
    <div>
      {awaiting.map(({ result, binding, binding_digest, binding_analysis, binding_problem }) =>
        binding !== null && binding_digest !== null ? (
          <BindingReview
            key={result.check_id}
            checkId={result.check_id}
            binding={binding}
            digest={binding_digest}
            analysis={binding_analysis}
            problem={binding_problem}
            onDecide={(action, comment) => onDecide(binding_digest, action, comment)}
          />
        ) : null,
      )}
      {broken.map(({ result, binding_problem }) => (
        <p key={result.check_id} className="warning">
          <code>{result.check_id}</code>: {binding_problem}
        </p>
      ))}
    </div>
  );
}
