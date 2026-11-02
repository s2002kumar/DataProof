import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import analysisExample from "../../../packages/contracts/examples/BindingAnalysis.reads-unchanged-orders.json";
import { BindingReview } from "../components/BindingReview";
import type { BindingAnalysis, CandidateBinding } from "../lib/types";

afterEach(cleanup);

const hostile = "SELECT o.order_id, sum(l.amount) FROM ledger_entries l JOIN orders o USING (order_id)\n-- <img src=x onerror=alert(1)><script>alert(1)</script>";
const binding: CandidateBinding = {
  source_path: "dataproof/bindings/c2_refund_totals.toml",
  head_sha: "8f3ab2a3b2ce14772811945b1f7ea036b769932e",
  query_sql: hostile,
  api_route: null,
  column_mapping: { order_id: "order_id", refund_total: "sum" },
};
const analysis = analysisExample as BindingAnalysis;

function renderReview(onDecide = vi.fn(async () => undefined)) {
  const view = render(
    <BindingReview
      checkId="commerce.c2_refund_totals"
      binding={binding}
      digest={analysis.binding_digest}
      analysis={analysis}
      problem={null}
      onDecide={onDecide}
    />,
  );
  return { ...view, onDecide };
}

it("shows the pull request's SQL as text, never as markup", () => {
  const { container } = renderReview();
  expect(container.querySelector("pre.sql")?.textContent).toBe(hostile);
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
});

it("flags relations the pull request did not create or alter", () => {
  renderReview();
  expect(screen.getByRole("note").textContent).toContain("public.orders");
  expect(screen.getByText("public.ledger_entries").closest("li")?.textContent).toContain(
    "created by this pull request",
  );
  expect(screen.getByText(/does not approve the migration/)).toBeTruthy();
});

it("sends the reviewer's decision with their comment", async () => {
  const { onDecide } = renderReview();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Reads the new ledger. " } });
  fireEvent.click(screen.getByRole("button", { name: "Approve binding" }));
  await waitFor(() => expect(onDecide).toHaveBeenCalledWith("APPROVE_BINDING", "Reads the new ledger."));
});

it("shows why a decision was refused", async () => {
  renderReview(
    vi.fn(async () => {
      throw new Error("the pull request's author cannot approve its bindings");
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Reject binding" }));
  expect((await screen.findByRole("alert")).textContent).toContain("cannot approve");
});
