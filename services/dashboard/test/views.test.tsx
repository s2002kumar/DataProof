import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import checkExample from "../../../packages/contracts/examples/CheckResult.f1-detected.json";
import investigationExample from "../../../packages/contracts/examples/Investigation.f1-diagnosis.json";
import reproductionExample from "../../../packages/contracts/examples/Reproduction.f1-refund.json";
import { Checks } from "../components/Checks";
import { Findings, Reproductions } from "../components/Findings";
import { RunList } from "../components/RunList";
import type { CheckResult, Investigation, Reproduction, RunSummary } from "../lib/types";

afterEach(cleanup);

it("a failed check shows each mismatch with expected and observed values", () => {
  const result = checkExample as unknown as CheckResult;
  render(
    <Checks
      checks={[{ result, binding: null, binding_digest: null, binding_analysis: null, binding_problem: null }]}
    />,
  );
  const row = screen.getByText("1002").closest("tr");
  expect(row?.textContent).toBe("1002amount49.9999.98");
  expect(screen.getByText(/ev-c1-amount-query/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Approve binding" })).toBeNull();
});

it("findings are labelled advisory and show what they cite", () => {
  render(<Findings investigations={[investigationExample as unknown as Investigation]} />);
  expect(screen.getByText("Advisory")).toBeTruthy();
  expect(screen.getByRole("link", { name: "investigation.tool.1" }).getAttribute("href")).toMatch(/-tool-1$/);
  expect(screen.getByText(/Only the deterministic checks decide the verdict/)).toBeTruthy();
});

it("a reproduction shows its outcome and the subset it used", () => {
  render(<Reproductions reproductions={[reproductionExample as unknown as Reproduction]} />);
  expect(screen.getByText("REPRODUCED")).toBeTruthy();
});

it("runs link to their page and show their verdict", () => {
  const run: RunSummary = {
    run_id: "6f1d2c3e-0a4b-4c5d-8e9f-0123456789ab",
    repository: "example-org/commerce-demo",
    pull_request: 7,
    head_sha: "8f3ab2a3b2ce14772811945b1f7ea036b769932e",
    trigger: "webhook",
    parent_run_id: null,
    state: "READY_FOR_REVIEW",
    verdict: "BLOCKED",
    conclusion: "failure",
    created_at: "2026-11-02T10:00:00Z",
  };
  render(<RunList runs={[run, { ...run, run_id: "7f1d2c3e-0a4b-4c5d-8e9f-0123456789ab", pull_request: null, verdict: null }]} />);
  expect(screen.getByText("6f1d2c3e").getAttribute("href")).toBe(`/runs/${run.run_id}`);
  expect(screen.getByText("BLOCKED")).toBeTruthy();
  expect(screen.getByText("manual")).toBeTruthy();
  expect(screen.getByText("not reported")).toBeTruthy();
});

it("a failed data check offers a reproduction; other checks do not", () => {
  const failed = checkExample as unknown as CheckResult;
  const passed = { ...failed, check_id: "c2.total", outcome: "PASS" as const, mismatch_count: 0, mismatch_examples: [] };
  const onReproduce = vi.fn();
  render(
    <Checks
      checks={[
        { result: failed, binding: null, binding_digest: null, binding_analysis: null, binding_problem: null },
        { result: passed, binding: null, binding_digest: null, binding_analysis: null, binding_problem: null },
      ]}
      onReproduce={onReproduce}
    />,
  );
  const buttons = screen.getAllByRole("button", { name: "Reproduce on a small subset" });
  expect(buttons).toHaveLength(1);
  fireEvent.click(buttons[0]);
  expect(onReproduce).toHaveBeenCalledWith("c1.amount");
});

it("a queued or failed reproduction request is shown with its state", () => {
  render(
    <Reproductions
      reproductions={[]}
      requests={[
        {
          request_id: "r1",
          check_id: "c1.amount",
          requested_by: "maintainer",
          requested_at: "2026-11-02T10:00:00Z",
          state: "failed",
          reproduction_id: null,
          failure: "c1.amount did not fail",
        },
      ]}
    />,
  );
  expect(screen.getByText(/failed, requested by maintainer/)).toBeTruthy();
  expect(screen.getByText(/did not fail/)).toBeTruthy();
});
