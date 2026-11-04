# dataproof-report

Renders a run's report as the Markdown a reviewer reads on GitHub. The GitHub App uses it for every check run's summary ([control plane API](../../services/api/README.md#check-runs)); the GitHub Action uses it for the job summary.

The input is the report mapping the runner records: `coverage` rows (check ID, requiredness, outcome, reason, counts and bounded typed mismatch examples), `evidence` digests, `investigations`, and `not_executed_reason` for a refused run. The summary reads top down:

1. the verdict and what it means;
2. what to do next, for that verdict;
3. each failing check, with bounded expected-versus-observed examples by key and column;
4. the coverage table with a tally, naming unsupported checks (never counted as passed);
5. the evidence digests;
6. the rule-based diagnosis.

**Trusted content only.** No text from the pull request and no model output appears. A mismatch example's observed value comes from the candidate database, which the pull request's migration wrote, so every value is escaped for Markdown and HTML (`|`, backticks, `<`, `>`, `&`) and bounded to 60 characters, at most 5 examples are shown per check, and the diagnosis is limited to 6 statements of 400 characters. A 60-check report with 50 examples each stays under GitHub's 65,535-character limit (`tests/unit/test_check_summary.py`).
