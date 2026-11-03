import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import checkExample from "../../../packages/contracts/examples/CheckResult.f1-detected.json";
import investigationExample from "../../../packages/contracts/examples/Investigation.f1-diagnosis.json";
import { BindingReviews, Checks } from "../components/Checks";
import { Findings } from "../components/Findings";
import { Journey } from "../components/Journey";
import { Sql, tokenize } from "../components/Sql";
import { journey, nextStep } from "../lib/journey";
import type { CheckRecord, CheckResult, Investigation, RunDetail } from "../lib/types";

afterEach(cleanup);

const failed = checkExample as unknown as CheckResult;
const diagnosis = investigationExample as unknown as Investigation;
const record = (result: CheckResult, extra: Partial<CheckRecord> = {}): CheckRecord => ({
  result,
  binding: null,
  binding_digest: null,
  binding_analysis: null,
  binding_problem: null,
  ...extra,
});
const passed = record({ ...failed, check_id: "c2.total", outcome: "PASS", mismatch_count: 0, mismatch_examples: [] });
const awaiting = record(
  { ...failed, check_id: "c3.capture", outcome: "NOT_RUN", not_run_reason: "AWAITING_BINDING_REVIEW", mismatch_count: 0, mismatch_examples: [] },
  {
    binding: {
      source_path: "dataproof/bindings/c3.toml",
      head_sha: "8f3ab2a3b2ce14772811945b1f7ea036b769932e",
      query_sql: "SELECT order_id, sum(amount) FROM ledger_entries GROUP BY order_id",
      api_route: null,
      column_mapping: { order_id: "order_id" },
    },
    binding_digest: "d".repeat(64),
  },
);
const analysis: Investigation = {
  ...diagnosis,
  investigation_id: "a1",
  role: "CHANGE_ANALYST",
  model: "dataproof:risk-rules-v1",
  tool_calls: [],
  hypotheses: [
    {
      statement: "join-in-backfill at sample-app/migrations/versions/x.py:19-30: A backfill reads through a join.",
      cited_evidence_ids: ["source:sample-app:migrations:versions:x.py:L19-L30"],
      confidence: "MEDIUM",
    },
  ],
  suggested_action: "A parent with several child rows (join-in-backfill).",
};

function run(verdict: "BLOCKED" | "INCOMPLETE" | "CHECKED", investigations: Investigation[]): RunDetail {
  return {
    run: {
      run_id: "6f1d2c3e-0a4b-4c5d-8e9f-0123456789ab",
      inputs: {
        repository: "example-org/commerce-demo",
        pull_request: 7,
        base_sha: "a".repeat(40),
        head_sha: "b".repeat(40),
        head_repository: null,
        config_digest: "c".repeat(64),
      },
      trigger: "webhook",
      parent_run_id: null,
      state: "READY_FOR_REVIEW",
      error_code: null,
      not_executed_reason: null,
      superseded_by: null,
      created_at: "2026-11-03T10:00:00Z",
    },
    pull_request_author: "developer",
    transitions: [],
    report: {
      verdict,
      conclusion: "failure",
      report: {
        coverage: [
          { check_id: failed.check_id, required: true, outcome: verdict === "CHECKED" ? "PASS" : "FAIL", reason: null, mismatch_count: 1 },
          { check_id: "c2.total", required: true, outcome: "PASS", reason: null, mismatch_count: 0 },
        ],
      },
    },
    decisions: [],
    investigations,
    reproductions: [],
    reproduction_requests: [],
    previews: {},
    check_run: null,
    check_run_omitted_for: null,
  };
}

it("a blocked run reads as risk, failed verification, evidence, diagnosis, then a correction", () => {
  const steps = journey(run("BLOCKED", [analysis, diagnosis]), [record(failed), passed]);
  expect(steps.map((step) => [step.title, step.status, step.tone])).toEqual([
    ["Change risk", "1 risk named (rules)", "warn"],
    ["Verification", "1 of 2 required checks passed", "bad"],
    ["Failure evidence", `1 check failed, ${failed.mismatch_count} ${failed.mismatch_count === 1 ? "mismatch" : "mismatches"}`, "bad"],
    ["Diagnosis", "Rule-based: 1 hypothesis", "neutral"],
    ["Review and correction", "Fix, then replay a correction", "warn"],
  ]);
  expect(nextStep(steps)?.id).toBe("evidence");
});

it("an incomplete run points the reviewer at its bindings", () => {
  const steps = journey(run("INCOMPLETE", []), [awaiting, passed]);
  expect(steps[0].status).toBe("Not analysed");
  expect(steps[3].status).toBe("Not needed");
  expect(steps[4].status).toBe("1 binding to review");
  expect(nextStep(steps)?.id).toBe("decision");
});

it("the steps say their status in words and mark the next one for assistive technology", () => {
  const steps = journey(run("INCOMPLETE", []), [awaiting, passed]);
  render(<Journey steps={steps} next={nextStep(steps)} />);
  const current = screen.getByRole("link", { current: "step" });
  expect(current.getAttribute("href")).toBe("#decision");
  expect(current.textContent).toContain("1 binding to review (needs attention)");
  expect(screen.getByRole("navigation", { name: "Review steps" })).toBeTruthy();
});

it("SQL is highlighted by token, and its text is exactly the original", () => {
  const sql = "SELECT o.order_id, sum(l.amount) -- note\nFROM ledger_entries l WHERE kind = 'REFUND' AND amount > 49.99";
  expect(tokenize(sql).map((token) => token.text).join("")).toBe(sql);
  const { container } = render(<Sql sql={sql} />);
  expect(container.querySelector("pre.sql")?.textContent).toBe(sql);
  expect([...container.querySelectorAll(".sql-keyword")].map((node) => node.textContent)).toContain("FROM");
  expect(container.querySelector(".sql-string")?.textContent).toBe("'REFUND'");
  expect(container.querySelector(".sql-comment")?.textContent).toBe("-- note");
});

it("a diagnosis links its citations to the check evidence and to what it read", () => {
  render(<Findings investigations={[diagnosis]} evidenceHref={(id) => (id.startsWith("commerce.") ? "#check-x" : null)} />);
  const [checkLink, toolLink] = screen.getAllByRole("link");
  expect(checkLink.getAttribute("href")).toBe("#check-x");
  expect(toolLink.getAttribute("href")).toBe(`#${diagnosis.investigation_id}-tool-1`);
  expect(screen.getByText(/What it read: 1 tool call/)).toBeTruthy();
  expect(document.getElementById(`${diagnosis.investigation_id}-tool-1`)?.textContent).toContain("run_readonly_query");
});

it("a change analysis names what produced it, when, and its edge cases", () => {
  render(<Findings investigations={[analysis, { ...analysis, investigation_id: "a2", status: "NOT_AVAILABLE", hypotheses: [], suggested_action: null, caveat: null, failure: "no risk rule matched the changed source" }]} />);
  expect(screen.getAllByText("Static risk rules (no model)")).toHaveLength(2);
  expect(screen.getAllByText(/before the migration ran/)).toHaveLength(2);
  expect(screen.getByText("Edge cases to cover:")).toBeTruthy();
  expect(screen.getByText(/Nothing to report: no risk rule matched/)).toBeTruthy();
});

it("failed checks come first, a check awaiting review points to the decision, and passes fold away", () => {
  const { container } = render(<Checks checks={[passed, awaiting, record(failed)]} />);
  const order = [...container.querySelectorAll("article.check")].map((card) => card.id);
  expect(order).toEqual([`check-${failed.check_id}`, "check-c3.capture", "check-c2.total"]);
  expect(container.querySelector("details.passed summary")?.textContent).toBe("1 check passed");
  expect(screen.getByRole("link", { name: /review its binding in step 5/ }).getAttribute("href")).toBe("#decision");
  expect(screen.queryByRole("button", { name: "Approve binding" })).toBeNull();
});

it("every binding awaiting review gets its own review", () => {
  const { container } = render(<BindingReviews checks={[awaiting, passed]} onDecide={vi.fn()} />);
  expect(container.querySelectorAll("section.binding")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Approve binding" })).toBeTruthy();
  expect(container.querySelector("pre.sql")?.textContent).toBe(awaiting.binding?.query_sql);
});
