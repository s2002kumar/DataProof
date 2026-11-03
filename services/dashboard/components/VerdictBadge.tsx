import type { Verdict } from "@/lib/types";

/** What each verdict means, in one sentence. */
export const MEANINGS: Record<Verdict, string> = {
  BLOCKED: "A required check failed or errored",
  INCOMPLETE: "A required check did not run (for example, awaiting binding review)",
  NOT_EXECUTED: "Refused by policy before anything ran",
  CHECKED: "Every required check passed",
  CHECKED_WITH_WARNINGS: "Every required check passed, with warnings",
};

export function VerdictBadge({ verdict }: { verdict: Verdict | null }) {
  if (verdict === null) return <span className="badge pending">not reported</span>;
  return (
    <span className={`badge ${verdict.toLowerCase()}`} title={MEANINGS[verdict]}>
      {verdict}
    </span>
  );
}
