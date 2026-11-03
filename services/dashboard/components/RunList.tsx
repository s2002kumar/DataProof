import Link from "next/link";

import { formatTime, shortSha } from "@/lib/format";
import type { RunSummary } from "@/lib/types";
import { VerdictBadge } from "./VerdictBadge";

export function RunList({ runs }: { runs: RunSummary[] }) {
  if (runs.length === 0) {
    return <p className="muted">No runs yet in the repositories you can read.</p>;
  }
  return (
    <div className="table-scroll" role="region" aria-label="Runs" tabIndex={0}>
    <table className="runs">
      <caption>Newest first. Open a run to review it step by step.</caption>
      <thead>
        <tr>
          <th scope="col">Run</th>
          <th scope="col">Repository</th>
          <th scope="col">Pull request</th>
          <th scope="col">Head</th>
          <th scope="col">Trigger</th>
          <th scope="col">State</th>
          <th scope="col">Verdict</th>
          <th scope="col">Created</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.run_id}>
            <td>
              <Link href={`/runs/${run.run_id}`}>{run.run_id.slice(0, 8)}</Link>
            </td>
            <td>{run.repository}</td>
            <td>{run.pull_request === null ? "manual" : `#${run.pull_request}`}</td>
            <td>
              <code>{shortSha(run.head_sha)}</code>
            </td>
            <td>{run.trigger}</td>
            <td>{run.state}</td>
            <td>
              <VerdictBadge verdict={run.verdict} />
            </td>
            <td>{formatTime(run.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
}
