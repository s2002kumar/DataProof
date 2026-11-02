"use client";

import { useState } from "react";

import type { BindingAnalysis, CandidateBinding } from "@/lib/types";

const PROVENANCE: Record<string, string> = {
  CREATED: "created by this pull request",
  ALTERED: "altered by this pull request",
  UNCHANGED: "pre-existing and untouched by this pull request",
};

export interface BindingReviewProps {
  checkId: string;
  binding: CandidateBinding;
  digest: string;
  analysis: BindingAnalysis | null;
  problem: string | null;
  onDecide: (action: "APPROVE_BINDING" | "REJECT_BINDING", comment: string) => Promise<void>;
}

/** What a binding reads, so the reviewer decides on exactly what will run (ADR-0005). */
export function BindingReview({ checkId, binding, digest, analysis, problem, onDecide }: BindingReviewProps) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function decide(action: "APPROVE_BINDING" | "REJECT_BINDING") {
    setBusy(true);
    setError(null);
    try {
      await onDecide(action, comment.trim());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const flagged = analysis?.relations.filter((relation) => relation.provenance === "UNCHANGED") ?? [];
  return (
    <section className="binding" aria-label={`Binding review for ${checkId}`}>
      <h4>Binding awaiting review</h4>
      <p className="muted">
        Proposed in <code>{binding.source_path}</code> at <code>{binding.head_sha.slice(0, 12)}</code>, digest{" "}
        <code>{digest.slice(0, 12)}</code>. Approving lets a new run read the candidate this way. It does not approve
        the migration, change a result or deploy anything.
      </p>
      {binding.query_sql !== null && <pre className="sql">{binding.query_sql}</pre>}
      {binding.api_route !== null && (
        <p>
          API route: <code>{binding.api_route}</code>
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Oracle column</th>
            <th>Candidate column</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(binding.column_mapping).map(([oracle, candidate]) => (
            <tr key={oracle}>
              <td>
                <code>{oracle}</code>
              </td>
              <td>
                <code>{candidate}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {analysis !== null && (
        <>
          <h5>Relations it reads</h5>
          <ul>
            {analysis.relations.map((relation) => (
              <li key={relation.name} className={relation.provenance === "UNCHANGED" ? "flagged" : undefined}>
                <code>{relation.name}</code>: {PROVENANCE[relation.provenance] ?? relation.provenance}
                {relation.through_views.length > 0 && <> (through {relation.through_views.join(", ")})</>}
              </li>
            ))}
          </ul>
          {flagged.length > 0 && (
            <p className="warning" role="note">
              Flagged: this binding reads {flagged.map((relation) => relation.name).join(", ")}, which this pull request
              did not create or alter. A binding that reads old data can pass while the new schema is wrong; check that
              it should.
            </p>
          )}
          <p className="muted">Functions: {analysis.functions.length > 0 ? analysis.functions.join(", ") : "none"}</p>
        </>
      )}
      {problem !== null && <p className="warning">{problem}</p>}
      <label>
        Comment (optional)
        <textarea value={comment} maxLength={2000} onChange={(event) => setComment(event.target.value)} />
      </label>
      <div className="actions">
        <button type="button" disabled={busy} onClick={() => decide("APPROVE_BINDING")}>
          Approve binding
        </button>
        <button type="button" disabled={busy} className="secondary" onClick={() => decide("REJECT_BINDING")}>
          Reject binding
        </button>
      </div>
      {error !== null && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
