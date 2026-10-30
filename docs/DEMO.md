# DEMO — 90-Second Walkthrough

## One-line pitch

> DataProof tests database migrations in isolated copies of an application, catches silent data corruption, and produces evidence-backed AI explanations before a developer approves the change.

## Script and artifacts

**0–12s — Show the problem:** Open GitHub PR changing commerce orders/refunds SQL. Point at green SQL execution alone not proving data correctness.

**12–30s — Live product:** DataProof shows reference and candidate preview URLs early. Demonstrate actual frontend/API/PG, identical baseline digest, and run SHA. When the candidate introduces its ledger table/endpoint mapping, the first run completes `INCOMPLETE` (non-green); show the explicit **`APPROVE_BINDING`** human action as a short annotated step, then the new run it starts. No step silently blesses a PR-provided comparator.

**30–50s — Valid migration:** Select a second safe migration or cached completed safe run (explicitly label recorded vs live). Its declared invariants and selected API behavior checks pass; show exact coverage, not a generic 10/10.

**50–72s — Silent corruption:** F1 grouped-join migration **successfully executes**, preserves row count and event key identity, but changes a $49.99 refund to a single $99.98 ledger entry. Show exact event ID, affected order, source SQL and failing C1/C2. Note that a configured amount-SUM baseline would also catch this particular fault; use F3 in benchmark to show more discriminating value-check benefits.

**72–85s — AI investigation:** Open grounded analysis with cited query evidence and hypothesis. If a verified smaller fixture exists, show it replaying the same failure. Optionally demonstrate developer-selected correction replay in a recorded clip.

**85–90s — Developer decision:** Show GitHub commit-bound status and downloadable evidence report; remind that production merge/deploy remains human-controlled.

## Evidence the published video must contain

A real uncropped path or code-commit link proving DB migration ran; actual preview app; deterministic mismatches; AI tool trace; check results; GitHub commit SHA; a clean rerun and resource cleanup. Clearly annotate any prerecorded steps or accelerated playback. Do not fabricate UI or metric screenshots.

## README ordering after implementation

Hero line → 30–90s demo → one screenshot for failing integrity report → short 'How it works' diagram → local quickstart → test & benchmark outcomes → tradeoffs and limitations → source credit. A first-time reader can understand it in 10 seconds and inspect engineering depth afterward.
