"""What the second evaluation's development stage showed, and the changes it led to.

Each test holds one change made after reading the 18 development investigations (the pilot's
two and the stage's sixteen), before the held-out stage. None of them decides an acceptance
criterion: the owner's marks decide the cause and the statement, and the Change Analyst is
still graded by ``located``, as the protocol says.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from dataproof_benchmark import ablation, evaluation, files
from dataproof_benchmark.corpus import CORPUS_SEED, build_corpus

MIGRATION = '''\
"""Move legacy payment events into a per-order ledger."""

from alembic import op

revision = "6ba15146fff0"
down_revision = "0001"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ledger_entries (order_id bigint, amount numeric(18, 2))
        """
    )
    op.execute(
        """
        INSERT INTO ledger_entries (order_id, amount)
        SELECT e.order_id, e.amount
        FROM legacy_payment_events AS e
        """
    )
    op.execute(
        """
        UPDATE ledger_entries
        SET amount = CASE order_id WHEN 1318 THEN 233.96 ELSE 601.60 END
        WHERE order_id IN (1318, 1319)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ledger_entries")
'''
PATH = "sample-app/migrations/versions/6ba15146fff0_ledger_backfill.py"


def label(fault: tuple[int, int] = (22, 28)) -> dict[str, Any]:
    statements = evaluation.simple_statements(MIGRATION)
    return {
        "path": PATH,
        "fault_lines": fault,
        "statements": statements,
        "fault_statements": evaluation.at_fault(statements, list(range(fault[0], fault[1] + 1))),
    }


def test_the_statements_of_a_migration_are_its_simple_statements() -> None:
    statements = evaluation.simple_statements(MIGRATION)
    assert statements is not None
    # The docstring, the import, two assignments, three calls in upgrade(), one in downgrade().
    assert statements == [(1, 1), (3, 3), (5, 5), (6, 6), (10, 14), (15, 21), (22, 28), (32, 32)]
    assert not any(start <= 9 <= end for start, end in statements), "a definition is not one"
    assert evaluation.simple_statements("def broken(:\n") is None
    assert evaluation.at_fault(statements, [26]) == [(22, 28)]
    assert evaluation.at_fault(statements, [1000]) is None


def test_an_answer_that_names_its_context_after_the_fault_points_at_the_statement() -> None:
    """dev-101's answer: the UPDATE's lines, then the INSERT before it as context. The
    line-range proxy fails it; the first range it states is the statement at fault."""
    answer = (
        f"The UPDATE in {PATH}, lines 22-29, swaps the two amounts. The INSERT ... SELECT "
        "before it (lines 15-21) had copied e.amount over unchanged."
    )
    fault = label((22, 29))
    assert evaluation.located([answer], fault["fault_lines"], PATH) is False
    assert evaluation.statement_located([answer], fault) is True


def test_an_answer_that_starts_with_its_context_does_not_point_at_the_statement() -> None:
    """dev-111's answer named the correct backfill first: the first range is what counts."""
    answer = f"Run after the correct backfill at lines 15-21 of {PATH}, lines 22-29 swap them."
    assert evaluation.statement_located([answer], label((22, 29))) is False


@pytest.mark.parametrize(
    "answer",
    [
        "The fault is somewhere in upgrade(), lines 9-30.",  # reaches every statement
        "Lines 15-29 hold both statements.",  # reaches the INSERT too
        "See line 5.",  # an assignment
        "Nothing points at a line here.",  # no range at all
    ],
)
def test_a_range_that_reaches_another_statement_or_none_does_not_count(answer: str) -> None:
    assert evaluation.statement_located([answer], label((22, 29))) is False


def test_a_range_of_another_file_is_passed_over_and_the_suggested_action_is_read_next() -> None:
    texts = [
        "The model is defined at line 40 of sample-app/app/models.py.",
        f"Remove the UPDATE at lines 22-29 of {PATH}.",
    ]
    assert evaluation.statement_located(texts, label((22, 29))) is True


def test_a_constant_range_is_right_only_where_the_fault_happens_to_be() -> None:
    constant = "Lines 24-26."
    assert evaluation.statement_located([constant], label((22, 29))) is True
    assert evaluation.statement_located([constant], label((17, 19))) is False


def test_no_statement_at_fault_is_not_graded() -> None:
    assert evaluation.statement_located(["lines 22-29"], {"path": PATH}) is None


def test_the_development_labels_carry_the_statements_at_fault(tmp_path: Path) -> None:
    labels = evaluation.DevSet(tmp_path).labels()
    # dev-106: the foreign key the CREATE TABLE no longer has, and the UPDATE; not the INSERT
    # between them, though the first and last changed lines are on either side of it.
    assert labels["dev-106"]["fault_statements"] == [(16, 28), (39, 41)]
    assert labels["dev-101"]["fault_statements"] == [(39, 46)]
    clean = [c for c, lab in labels.items() if lab["kind"] == "clean"]
    assert clean
    assert all(labels[c]["fault_statements"] is None for c in clean)


def test_every_held_out_fault_has_its_statements_at_fault_within_its_lines() -> None:
    """The proxy can grade every held-out fault, inside the lines the owner is shown. Nothing
    about any answer is read here."""
    labels = evaluation.HeldoutSet().labels()
    faults = [lab for lab in labels.values() if lab["kind"] == "fault"]
    assert faults
    for lab in faults:
        assert lab["fault_statements"], "a held-out fault the proxy could not grade"
        low, high = lab["fault_lines"]
        assert all(low <= s and e <= high for s, e in lab["fault_statements"])


def test_a_term_of_the_cases_own_rubric_is_no_sign_of_a_hedge() -> None:
    """dev-108's answer: an event deleted and another duplicated. "missing" is also a partial
    migration's term and "duplicated" a retry's, so every correct answer was a hedge."""
    own = ablation.RUBRIC["balanced loss and duplication"]
    others = [
        groups
        for family, groups in ablation.RUBRIC.items()
        if family != "balanced loss and duplication"
    ]
    right = "The DELETE drops event 1221, so it is missing, and the INSERT leaves 1222 duplicated."
    assert evaluation.hedged([right], others) is True, "as it was graded"
    assert evaluation.hedged([right], others, own) is False
    hedge = right + " Or a join multiplied the rows, or the amounts were rounded."
    assert evaluation.hedged([hedge], others, own) is True


def test_rules_that_gave_no_answer_did_not_name_the_cause() -> None:
    """dev-111's check errored and the rules explain only failures, so they answered nothing:
    the by-case comparison used to leave the case out."""

    measured = {"citations": 0, "valid_citations": 0, "specifics": 0, "unsupported_specifics": []}
    rules = {
        **measured,
        "status": "NOT_AVAILABLE",
        "answered": False,
        "root_cause": None,
        "hedged": None,
        "located": None,
        "statement_located": None,
        "tool_calls": {},
        "cost_usd": "0",
        "latency_ms": 0.5,
    }
    model_run = {
        **measured,
        "status": "COMPLETED",
        "answered": True,
        "root_cause": True,
        "hedged": False,
        "located": False,
        "statement_located": True,
        "tool_calls": {"OK": 1},
        "cost_usd": "0.05",
        "latency_ms": 1000.0,
    }
    case = {
        "rules": rules,
        "runs": [model_run],
        "model": {
            "answered": True,
            "root_cause": True,
            "located": False,
            "statement_located": True,
        },
        "skipped": None,
    }
    summary = evaluation.summarize([case])
    assert summary["by_case"]["cases_with_a_rubric"] == 1
    assert summary["by_case"]["model_only"] == 1
    assert summary["by_case"]["model_statement_located"] == 1


def test_a_transcript_keeps_a_tool_result_whole() -> None:
    """Three development briefings were cut at 16,000 characters, by about 200 bytes each."""
    from dataproof_investigator import ToolResult
    from dataproof_investigator.tools import MAX_RESULT_BYTES

    assert ablation.TRANSCRIPT_RESULT_CHARS == MAX_RESULT_BYTES
    content = json.dumps({"evidence_id": "investigation.tool.1", "data": "x" * 16_206})
    kept = ablation._kept(ToolResult(tool_use_id="t", content=content, is_error=False))
    assert kept["truncated"] is False
    assert kept["content"] == content


def test_the_development_labels_record_the_seed_their_cases_were_built_with(tmp_path: Path) -> None:
    evaluation.DevSet(tmp_path)
    written = json.loads((tmp_path / "corpus-dev-20261119" / "labels.json").read_text("utf-8"))
    assert written["seed"] == evaluation.DEV_SEED == 20261119
    # The first corpus, which is frozen, still records its own.
    frozen = json.loads(files.rendered(build_corpus(), [])["labels.json"])
    assert frozen["seed"] == CORPUS_SEED


def test_a_report_of_a_stage_run_before_the_statement_proxy_still_renders() -> None:
    """The development stages' sealed results have no statement proxy; their report says so
    instead of failing."""
    measured = {"citations": 1, "valid_citations": 1, "specifics": 0, "unsupported_specifics": []}
    graded = {
        **measured,
        "status": "COMPLETED",
        "answered": True,
        "root_cause": True,
        "hedged": False,
        "located": False,
        "tool_calls": {"OK": 1},
        "cost_usd": "0.05",
        "latency_ms": 1000.0,
    }
    case = {
        "rules": graded,
        "runs": [graded],
        "model": {"answered": True, "root_cause": True, "located": False},
        "skipped": None,
    }
    summary = evaluation.summarize([case])
    for arm in ("rules", "model"):
        del summary[arm]["statement_located_of_answered"]
    del summary["by_case"]["model_statement_located"]
    document = {
        "set": "dev",
        "dry_run": False,
        "model": "anthropic:claude-opus-5-5",
        "effort": "medium",
        "prompt_version": "failure-investigator/3",
        "tool_version": "tools/3",
        "reps": 1,
        "budget": {
            "case_cap_usd": "1.00",
            "max_tool_calls": 12,
            "max_turns": 16,
            "approved_usd": "1.00",
        },
        "summary": summary,
        "cases": [],
    }
    shown = evaluation.report(document)
    assert (
        "| First range stated points at the faulty statement, of answers | not recorded |" in shown
    )
    assert "pointed first at the faulty statement in none recorded" in shown
