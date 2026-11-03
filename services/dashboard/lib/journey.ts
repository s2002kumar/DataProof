// The review as five steps, each summarised from the run's own records: what the change risks,
// what was verified, the evidence of what failed, the advisory diagnosis, and what a person
// decides. Nothing here decides anything; it only reads what the API returned.

import type { CheckRecord, Investigation, RunDetail } from "./types";

export type Tone = "good" | "warn" | "bad" | "neutral";

export interface Step {
  id: "risk" | "verification" | "evidence" | "diagnosis" | "decision";
  number: number;
  title: string;
  status: string;
  tone: Tone;
}

const plural = (count: number, word: string, words = `${word}s`) => `${count} ${count === 1 ? word : words}`;

export const isAnalysis = (investigation: Investigation) => investigation.role === "CHANGE_ANALYST";
export const isRuleBased = (investigation: Investigation) => investigation.model.startsWith("dataproof:");

export function failing(checks: CheckRecord[]): CheckRecord[] {
  return checks.filter(({ result }) => result.outcome === "FAIL" || result.outcome === "ERROR");
}

export function awaitingReview(checks: CheckRecord[]): CheckRecord[] {
  return checks.filter(
    ({ result, binding, binding_digest }) =>
      result.not_run_reason === "AWAITING_BINDING_REVIEW" && binding !== null && binding_digest !== null,
  );
}

function riskStep(run: RunDetail): Step {
  const analyses = run.investigations.filter(isAnalysis);
  const base = { id: "risk" as const, number: 1, title: "Change risk" };
  if (analyses.length === 0) return { ...base, status: "Not analysed", tone: "neutral" };
  const risks = analyses
    .filter((analysis) => analysis.status === "COMPLETED")
    .reduce((count, analysis) => count + analysis.hypotheses.length, 0);
  const by = analyses.some((analysis) => !isRuleBased(analysis)) ? "rules and AI" : "rules";
  return risks > 0
    ? { ...base, status: `${plural(risks, "risk")} named (${by})`, tone: "warn" }
    : { ...base, status: `No risk pattern found (${by})`, tone: "good" };
}

function verificationStep(run: RunDetail): Step {
  const base = { id: "verification" as const, number: 2, title: "Verification" };
  const report = run.report;
  if (report === null) return { ...base, status: "Not reported yet", tone: "neutral" };
  if (report.verdict === "NOT_EXECUTED") {
    const reason = report.report.not_executed_reason;
    return { ...base, status: `Not executed${reason ? `: ${reason}` : ""}`, tone: "warn" };
  }
  const required = (report.report.coverage ?? []).filter((row) => row.required);
  const passed = required.filter((row) => row.outcome === "PASS").length;
  const broken = required.some((row) => row.outcome === "FAIL" || row.outcome === "ERROR");
  const tone: Tone = broken ? "bad" : passed === required.length ? "good" : "warn";
  return { ...base, status: `${passed} of ${plural(required.length, "required check")} passed`, tone };
}

function evidenceStep(run: RunDetail, checks: CheckRecord[]): Step {
  const base = { id: "evidence" as const, number: 3, title: "Failure evidence" };
  const failed = failing(checks);
  if (failed.length > 0) {
    const mismatches = failed.reduce((count, { result }) => count + result.mismatch_count, 0);
    return {
      ...base,
      status: `${plural(failed.length, "check")} failed, ${plural(mismatches, "mismatch", "mismatches")}`,
      tone: "bad",
    };
  }
  if (run.report === null) return { ...base, status: "Waiting for the checks", tone: "neutral" };
  return { ...base, status: "No check failed", tone: "good" };
}

function diagnosisStep(run: RunDetail, checks: CheckRecord[]): Step {
  const base = { id: "diagnosis" as const, number: 4, title: "Diagnosis" };
  const diagnoses = run.investigations.filter((investigation) => !isAnalysis(investigation));
  if (diagnoses.length === 0) {
    return { ...base, status: failing(checks).length > 0 ? "Not run" : "Not needed", tone: "neutral" };
  }
  const shown = diagnoses.filter((diagnosis) => diagnosis.status === "COMPLETED");
  const by = diagnoses.some((diagnosis) => !isRuleBased(diagnosis)) ? "Rule-based and AI" : "Rule-based";
  const hypotheses = shown.reduce((count, diagnosis) => count + diagnosis.hypotheses.length, 0);
  return { ...base, status: `${by}: ${plural(hypotheses, "hypothesis", "hypotheses")}`, tone: "neutral" };
}

function decisionStep(run: RunDetail, checks: CheckRecord[]): Step {
  const base = { id: "decision" as const, number: 5, title: "Review and correction" };
  const awaiting = awaitingReview(checks).length;
  if (awaiting > 0) return { ...base, status: `${plural(awaiting, "binding")} to review`, tone: "warn" };
  if (run.report?.verdict === "BLOCKED") return { ...base, status: "Fix, then replay a correction", tone: "warn" };
  if (run.report?.verdict === "CHECKED" || run.report?.verdict === "CHECKED_WITH_WARNINGS") {
    return { ...base, status: "Merging is a person's decision", tone: "good" };
  }
  if (run.decisions.length > 0) return { ...base, status: `${plural(run.decisions.length, "decision")} recorded`, tone: "neutral" };
  return { ...base, status: "Nothing to decide yet", tone: "neutral" };
}

export function journey(run: RunDetail, checks: CheckRecord[]): Step[] {
  return [riskStep(run), verificationStep(run), evidenceStep(run, checks), diagnosisStep(run, checks), decisionStep(run, checks)];
}

/** The step that needs the reviewer next, if any: decisions first, then failures. */
export function nextStep(steps: Step[]): Step | null {
  const decision = steps.find((step) => step.id === "decision" && step.tone === "warn");
  if (decision !== undefined && steps.some((step) => step.id === "evidence" && step.tone === "bad")) {
    return steps.find((step) => step.id === "evidence") ?? null;
  }
  return decision ?? null;
}
