// Shapes of the control plane API's responses (services/api/README.md). The contract models in
// packages/contracts are the source of truth; these types cover what the dashboard reads.

export type Verdict = "BLOCKED" | "INCOMPLETE" | "NOT_EXECUTED" | "CHECKED" | "CHECKED_WITH_WARNINGS";
export type Role = "reference" | "candidate";

export interface Session {
  login: string;
  csrf_token: string;
  expires_at: string;
  repositories: string[];
}

export interface RunSummary {
  run_id: string;
  repository: string;
  pull_request: number | null;
  head_sha: string;
  trigger: string;
  parent_run_id: string | null;
  state: string;
  verdict: Verdict | null;
  conclusion: string | null;
  created_at: string;
}

export interface TypedValue {
  type: string;
  value?: unknown;
}

export interface Mismatch {
  key: TypedValue[];
  column: string | null;
  expected: TypedValue | null;
  observed: TypedValue | null;
}

export interface CheckResult {
  check_id: string;
  contract_id: string;
  required: boolean;
  outcome: "PASS" | "FAIL" | "ERROR" | "NOT_RUN";
  not_run_reason: string | null;
  error_code: string | null;
  expected_key_count: number | null;
  observed_key_count: number | null;
  mismatch_count: number;
  mismatch_examples: Mismatch[];
  evidence_ids: string[];
}

export interface CandidateBinding {
  source_path: string;
  head_sha: string;
  query_sql: string | null;
  api_route: string | null;
  column_mapping: Record<string, string>;
}

export interface BindingRelation {
  name: string;
  provenance: "CREATED" | "ALTERED" | "UNCHANGED";
  through_views: string[];
}

export interface BindingAnalysis {
  binding_digest: string;
  relations: BindingRelation[];
  functions: string[];
}

export interface CheckRecord {
  result: CheckResult;
  binding: CandidateBinding | null;
  binding_digest: string | null;
  binding_analysis: BindingAnalysis | null;
  binding_problem: string | null;
}

export interface Hypothesis {
  statement: string;
  cited_evidence_ids: string[];
  confidence: string;
}

export interface Investigation {
  investigation_id: string;
  role: string;
  status: string;
  model: string;
  hypotheses: Hypothesis[];
  suggested_action: string | null;
  caveat: string | null;
  failure: string | null;
}

export interface Reproduction {
  reproduction_id: string;
  check_id: string;
  status: "REPRODUCED" | "NOT_REPRODUCED";
  reason: string | null;
  subset: { table: string; rows: number }[];
}

export interface HumanDecision {
  decision_id: string;
  action: string;
  actor: string;
  decided_at: string;
  head_sha: string;
  comment: string | null;
  binding_digest: string | null;
  approver_separation: string | null;
}

export interface Transition {
  seq: number;
  from_state: string | null;
  to_state: string;
  actor: string;
  at: string;
  detail: Record<string, unknown>;
}

export interface CoverageRow {
  check_id: string;
  required: boolean;
  outcome: string;
  reason: string | null;
  mismatch_count: number;
}

export interface Report {
  verdict: Verdict;
  conclusion: string;
  report: { coverage?: CoverageRow[]; not_executed_reason?: string };
}

export interface RunDetail {
  run: {
    run_id: string;
    inputs: {
      repository: string;
      pull_request: number | null;
      base_sha: string;
      head_sha: string;
      head_repository: string | null;
      config_digest: string;
    };
    trigger: string;
    parent_run_id: string | null;
    state: string;
    error_code: string | null;
    not_executed_reason: string | null;
    superseded_by: string | null;
    created_at: string;
  };
  pull_request_author: string | null;
  transitions: Transition[];
  report: Report | null;
  decisions: HumanDecision[];
  investigations: Investigation[];
  reproductions: Reproduction[];
  previews: Record<string, { commit_sha: string; ready: boolean; failure: string | null }>;
  check_run: { id: number; url: string } | null;
}
