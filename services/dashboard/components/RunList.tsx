import Link from "next/link";

import { formatTime, shortSha } from "@/lib/format";
import type { RunSummary } from "@/lib/types";
import { VerdictBadge } from "./VerdictBadge";

export function RunList({ runs }: { runs: RunSummary[] }) {
  if (runs.length === 0) {
    return <p className="muted">No runs yet in the repositories you can read.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Run</th>
          <th>Repository</th>
          <th>Pull request</th>
          <th>Head</th>
          <th>Trigger</th>
          <th>State</th>
          <th>Verdict</th>
          <th>Created</th>
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
  );
}
