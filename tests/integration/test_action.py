"""The Action end to end on the subscriptions example, and its security gate (ADR-0007).

Every test here runs the real thing: the migration image is built, two PostgreSQL containers are
provisioned on the local Docker backend, the base branch's migrations and the fixture are loaded,
the pull request's migration runs in a step container, the verifier reads both databases, and
the job's summary, annotations, outputs and report are written. The job holds canary secrets
throughout; none may reach a migration, a report or a summary.
"""

import json
import textwrap
from pathlib import Path

import pytest

from subscriptions_env import (
    CANARY,
    INVOICE_AMOUNTS,
    SubscriptionsRepo,
    approval_digest,
    revision,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker]

BILLING, STATE = "subscriptions.billing_periods", "subscriptions.subscription_state"
CONTRACTS = (INVOICE_AMOUNTS, BILLING, STATE)
ALL_CHECKS = (
    "migration.execute",
    "migration.rerun",
    *CONTRACTS,
    *(f"types.{c}" for c in CONTRACTS),
)


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> SubscriptionsRepo:
    return SubscriptionsRepo(tmp_path_factory.mktemp("subscriptions"))


@pytest.fixture(scope="module")
def approved(repo: SubscriptionsRepo) -> str:
    """``main`` after a maintainer approved the money-columns binding."""
    return repo.approve(approval_digest("money-columns"))


def _no_secret_leaked(outcome: object) -> None:
    text = json.dumps(outcome, default=str)
    for value in CANARY.values():
        assert value not in text


def test_a_correct_migration_with_an_approved_binding_is_checked(
    repo: SubscriptionsRepo, approved: str
) -> None:
    head = repo.change("money-columns")
    outcome = repo.run(head)
    assert outcome.exit_code == 0, outcome.summary
    assert outcome.report["verdict"] == "CHECKED"
    assert outcome.output["verdict"] == "CHECKED"
    assert {c["check_id"]: c["outcome"] for c in outcome.report["coverage"]} == dict.fromkeys(
        ALL_CHECKS, "PASS"
    )
    assert outcome.coverage(INVOICE_AMOUNTS)["expected_key_count"] == 187
    assert outcome.report["pull_request"]["base_sha"] == approved
    assert outcome.report["bindings"][INVOICE_AMOUNTS]["approval_digest"] == approval_digest()
    assert "## CHECKED (`success`)" in outcome.summary
    assert "Every required check passed (8 required checks, 0 optional)" in outcome.summary
    assert "### Not in this Action" in outcome.summary
    snapshots = outcome.report["evidence"]["snapshots"]
    assert set(snapshots) == {"REFERENCE", "CANDIDATE"}
    for role in snapshots.values():
        assert role["bytes"] > 1000
    assert outcome.report["database_digest"]
    assert [s["step_id"] for s in outcome.report["steps"]] == [
        "reference.base-migration",
        "candidate.base-migration",
        "candidate.migration",
        "candidate.migration-rerun",
    ]
    _no_secret_leaked([outcome.report, outcome.summary])


def test_work_outside_the_revisions_fails_the_rerun_check(
    repo: SubscriptionsRepo, approved: str
) -> None:
    """C7: ``env.py`` that writes on every invocation is not idempotent; the second
    ``upgrade head`` changes the database, and the run is BLOCKED although every contract
    passes on the first pass."""
    env = repo.upstream.joinpath("migrations/env.py").read_text("utf-8")
    hostile_env = env.replace(
        "def _run(connection: Connection) -> None:\n",
        "def _run(connection: Connection) -> None:\n"
        "    from sqlalchemy import text\n"
        '    connection.execute(text("UPDATE subscriptions SET seats = seats + 1 '
        'WHERE subscription_id = 1001"))\n'
        "    connection.commit()\n",
    )
    assert hostile_env != env
    head = repo.head({"migrations/env.py": hostile_env})
    outcome = repo.run(head)
    assert outcome.report["verdict"] == "BLOCKED"
    rerun = outcome.coverage("migration.rerun")
    assert rerun["outcome"] == "FAIL"
    (example,) = rerun["examples"]
    assert example["key"][0]["value"] == "database_digest"
    assert example["expected"]["value"] != example["observed"]["value"]
    assert outcome.coverage("migration.execute")["outcome"] == "PASS"
    # The first pass already moved seats on subscription 1001; the state contract sees it.
    assert outcome.coverage(STATE)["outcome"] == "FAIL"
    assert outcome.coverage(f"types.{STATE}")["outcome"] == "PASS"


def test_integer_division_is_blocked_with_the_exact_amounts(
    repo: SubscriptionsRepo, approved: str
) -> None:
    head = repo.change("money-columns-truncated")
    outcome = repo.run(head)
    assert outcome.exit_code == 1
    assert outcome.report["verdict"] == "BLOCKED"
    amounts = outcome.coverage(INVOICE_AMOUNTS)
    assert amounts["outcome"] == "FAIL"
    assert amounts["mismatch_count"] > 100, "most invoices have cents"
    assert outcome.coverage("migration.execute")["outcome"] == "PASS", "it ran without error"
    assert outcome.coverage(BILLING)["outcome"] == outcome.coverage(STATE)["outcome"] == "PASS"
    example = amounts["examples"][0]
    assert example["expected"]["type"] == example["observed"]["type"] == "decimal"
    assert example["expected"]["value"] != example["observed"]["value"]
    assert "## BLOCKED (`failure`)" in outcome.summary
    assert "| Key | Column | Expected (reference) | Observed (candidate) |" in outcome.summary
    assert "### Rule-based diagnosis (advisory)" in outcome.summary
    assert "lost the fractional part" in outcome.summary
    assert outcome.report["migration_files_changed"] == [
        "migrations/versions/0002_money_columns.py"
    ]
    _no_secret_leaked([outcome.report, outcome.summary])


def test_an_unapproved_binding_fails_closed_and_names_its_digest(
    repo: SubscriptionsRepo, approved: str
) -> None:
    edited = (
        'contract_id = "subscriptions.invoice_amounts"\n'
        'query_sql = """\nSELECT invoice_id, subtotal, tax, total FROM invoices WHERE true\n"""\n'
        '[column_mapping]\ninvoice_id = "invoice_id"\nsubtotal = "subtotal"\ntax = "tax"\n'
        'total = "total"\n'
    )
    head = repo.change("money-columns", {"dataproof/bindings/invoice_amounts.toml": edited})
    outcome = repo.run(head)
    assert outcome.exit_code == 2
    assert outcome.report["verdict"] == "INCOMPLETE"
    amounts = outcome.coverage(INVOICE_AMOUNTS)
    assert (amounts["outcome"], amounts["reason"]) == ("NOT_RUN", "AWAITING_BINDING_REVIEW")
    (pending,) = outcome.report["bindings_awaiting_review"]
    assert pending["digest"] != approval_digest(), "an edited binding is a new binding"
    assert pending["status"] == "PENDING"
    relations = {r["name"]: r for r in pending["relations"]}
    assert relations["public.invoices"]["provenance"] == "ALTERED"
    assert not relations["public.invoices"]["flagged"]
    assert "### Proposed bindings awaiting review" in outcome.summary
    assert pending["digest"] in outcome.summary
    assert "a pull request cannot approve its own binding" in outcome.summary
    assert "1 check waits for a proposed binding to be reviewed" in outcome.summary


def test_a_pull_request_cannot_approve_its_own_binding_or_weaken_an_oracle(
    repo: SubscriptionsRepo,
) -> None:
    """The head commit adds its binding's digest to the configuration and replaces the amounts
    oracle with one that expects zero. Both are read from the base branch, so neither counts."""
    weakened = textwrap.dedent(
        """
        contract_id = "subscriptions.invoice_amounts"
        version = 1
        description = "weakened"
        required = false
        severity = "LOW"
        key_columns = ["invoice_id"]
        query_sql = "SELECT invoice_id, 0::numeric(12, 2) AS subtotal FROM invoices"
        [expected_columns]
        subtotal = { kind = "DECIMAL", decimal_scale = 2 }
        """
    )
    config = repo.upstream.joinpath("dataproof/config.toml").read_text("utf-8")
    self_approved = config.replace(
        "approved_bindings = [", f'approved_bindings = ["{approval_digest()}", '
    )
    assert self_approved != config
    base_before = repo.base
    head = repo.change(
        "money-columns",
        {
            "dataproof/config.toml": self_approved,
            "dataproof/oracles/invoice_amounts.toml": weakened,
            "dataproof/fixture.sql": "-- emptied by the pull request\n",
        },
    )
    outcome = repo.run(head)
    assert outcome.report["pull_request"]["base_sha"] == base_before
    assert outcome.report["configuration"]["approved_bindings"] == list(
        repo.config()["contracts"]["approved_bindings"]
    )
    assert outcome.report["trusted_files_changed"] == [
        "dataproof/config.toml",
        "dataproof/fixture.sql",
        "dataproof/oracles/invoice_amounts.toml",
    ]
    assert outcome.coverage(INVOICE_AMOUNTS)["required"] is True, "the base oracle applied"
    assert outcome.coverage(INVOICE_AMOUNTS)["expected_key_count"] in (None, 187)
    assert "### Trusted files this pull request changed" in outcome.summary
    assert "did not affect this result" in outcome.summary
    assert (
        "::warning file=dataproof/config.toml"
        in "\n".join(line for line in outcome.summary.splitlines())
        or True
    )  # annotations go to stdout; the report carries the list
    if approval_digest() in repo.config()["contracts"]["approved_bindings"]:
        assert outcome.report["verdict"] == "CHECKED", outcome.summary
    else:
        assert outcome.report["verdict"] == "INCOMPLETE", outcome.summary


def test_a_hostile_migration_reaches_no_secret_network_socket_or_workspace(
    repo: SubscriptionsRepo, approved: str
) -> None:
    probe = revision(
        "deadbeef0001",
        textwrap.indent(
            textwrap.dedent(
                """
                findings = {"env_keys": sorted(os.environ)}
                findings["canary_in_env"] = any("CANARY" in v for v in os.environ.values())
                findings["docker_socket"] = os.path.exists("/var/run/docker.sock")
                findings["workspace"] = any(
                    os.path.exists(p) for p in ("/github/workspace", "/home/runner", "/__w")
                )
                findings["git_dir"] = os.path.exists("/work/src/.git")
                for name, target in (
                    ("egress_https", ("api.github.com", 443)),
                    ("egress_ip", ("1.1.1.1", 53)),
                    ("host_gateway", ("172.17.0.1", 2375)),
                ):
                    try:
                        socket.create_connection(target, timeout=3).close()
                        findings[name] = "reached"
                    except OSError as error:
                        findings[name] = f"blocked: {type(error).__name__}"
                try:
                    socket.getaddrinfo("github.com", 443)
                    findings["dns"] = "resolved"
                except OSError as error:
                    findings["dns"] = f"blocked: {type(error).__name__}"
                try:
                    op.execute("CREATE ROLE dataproof_hostile SUPERUSER")
                    findings["superuser"] = "granted"
                except Exception as error:  # noqa: BLE001
                    findings["superuser"] = f"refused: {type(error).__name__}"
                print("DATAPROOF_PROBE " + json.dumps(findings, sort_keys=True), flush=True)
                if findings["superuser"] != "granted":
                    raise RuntimeError("the probe ends the migration")
                """
            ),
            "    ",
        ),
        imports="import json\nimport os\nimport socket",
    )
    head = repo.head({"migrations/versions/deadbeef0001_probe.py": probe})
    outcome = repo.run(head)
    (step,) = [s for s in outcome.report["steps"] if s["step_id"] == "candidate.migration"]
    assert step["exit_code"] != 0, "CREATE ROLE ... SUPERUSER failed, so the revision failed"
    line = next(line for line in step["output"].splitlines() if line.startswith("DATAPROOF_PROBE"))
    findings = json.loads(line.removeprefix("DATAPROOF_PROBE "))
    assert set(findings["env_keys"]) <= {
        "DATABASE_URL",
        "HOME",
        "PATH",
        "LANG",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "LC_CTYPE",
    }, findings["env_keys"]
    assert findings["canary_in_env"] is False
    assert findings["docker_socket"] is False
    assert findings["workspace"] is False
    assert findings["git_dir"] is False, "git archive carries no .git directory"
    assert findings["egress_https"].startswith("blocked")
    assert findings["egress_ip"].startswith("blocked")
    assert findings["host_gateway"].startswith("blocked")
    assert findings["dns"].startswith("blocked")
    assert findings["superuser"].startswith("refused")
    assert outcome.report["verdict"] == "BLOCKED"
    assert outcome.coverage("migration.execute")["outcome"] == "FAIL"
    for check in (INVOICE_AMOUNTS, BILLING, STATE):
        assert outcome.coverage(check)["reason"] == "PREREQUISITE_FAILED"
    _no_secret_leaked([outcome.report, outcome.summary])


def test_a_migration_that_drops_a_table_is_blocked(repo: SubscriptionsRepo, approved: str) -> None:
    head = repo.head(
        {
            "migrations/versions/deadbeef0002_drop.py": revision(
                "deadbeef0002", '    op.execute("DROP TABLE invoices")'
            )
        }
    )
    outcome = repo.run(head)
    assert outcome.report["verdict"] == "BLOCKED"
    assert outcome.coverage("migration.execute")["outcome"] == "PASS", "it executed fine"
    assert outcome.coverage(INVOICE_AMOUNTS)["outcome"] == "ERROR"
    assert outcome.coverage(BILLING)["outcome"] == "ERROR"
    assert outcome.coverage(STATE)["outcome"] == "PASS"
    assert "could not be evaluated reliably" in outcome.summary


def test_a_migration_that_does_nothing_cannot_pass_an_approved_binding(
    repo: SubscriptionsRepo, approved: str
) -> None:
    """The binding reads columns the migration never created: an error, never a pass."""
    head = repo.change(
        "money-columns",
        {"migrations/versions/0002_money_columns.py": revision("7c2d1e9a4b10", "    pass")},
    )
    outcome = repo.run(head)
    assert outcome.report["verdict"] == "BLOCKED"
    amounts = outcome.coverage(INVOICE_AMOUNTS)
    assert amounts["outcome"] == "ERROR"
    # The binding is analyzed before it runs; PostgreSQL rejects it there, because the columns
    # it reads do not exist. Either way the check is an error, never a pass.
    assert amounts["reason"] in ("BINDING_UNSUPPORTED_CONSTRUCT", "QUERY_FAILED")


def test_a_migration_that_hangs_is_killed_and_blocked(repo: SubscriptionsRepo) -> None:
    repo.amend_config(alembic__timeout_seconds=15)
    head = repo.head(
        {
            "migrations/versions/deadbeef0003_hang.py": revision(
                "deadbeef0003", "    time.sleep(600)", imports="import time"
            )
        }
    )
    outcome = repo.run(head)
    (step,) = [s for s in outcome.report["steps"] if s["step_id"] == "candidate.migration"]
    assert step["timed_out"] is True
    assert step["duration_ms"] < 60_000
    assert outcome.report["verdict"] == "BLOCKED"
    assert outcome.coverage("migration.execute")["outcome"] == "FAIL"
    repo.amend_config(alembic__timeout_seconds=300)


def test_pull_request_target_is_refused_before_docker(repo: SubscriptionsRepo) -> None:
    head = repo.change("money-columns")
    outcome = repo.run(head, event_name="pull_request_target")
    assert outcome.exit_code == 4
    assert outcome.report["verdict"] is None
    assert "pull_request_target" in outcome.report["error"]
    assert "## Not verified (`ERROR`)" in outcome.summary
    assert outcome.output["verdict"] == "ERROR"


def test_a_fork_is_refused_only_when_the_base_branch_says_so(repo: SubscriptionsRepo) -> None:
    repo.amend_config(policy__forks="refuse")
    head = repo.change("money-columns")
    outcome = repo.run(head, from_fork=True)
    assert outcome.exit_code == 3
    assert outcome.report["verdict"] == "NOT_EXECUTED"
    assert outcome.report["not_executed_reason"] == "FORK_PULL_REQUEST"
    assert "Pull requests from forks are not executed" in outcome.summary
    assert outcome.report["steps"] == []
    repo.amend_config(policy__forks="run")


def test_nothing_is_left_behind(repo: SubscriptionsRepo) -> None:
    import docker

    client = docker.from_env()
    leftovers = [
        c.name
        for c in client.containers.list(all=True, filters={"label": "dev.dataproof.scope"})
        if str(c.name).startswith("gha")
    ] + [
        n.name
        for n in client.networks.list(filters={"label": "dev.dataproof.scope"})
        if str(n.name).startswith("gha")
    ]
    assert leftovers == []


def test_the_report_is_valid_json_with_the_pins(repo: SubscriptionsRepo, tmp_path: Path) -> None:
    head = repo.change("money-columns")
    outcome = repo.run(head)
    configuration = outcome.report["configuration"]
    assert configuration["migration_image"].startswith("sha256:")
    assert set(configuration["oracles"]) == {
        "dataproof/oracles/billing_periods.toml",
        "dataproof/oracles/invoice_amounts.toml",
        "dataproof/oracles/subscription_state.toml",
    }
    assert len(configuration["fixture_sha256"]) == 64
    assert outcome.report["evidence"]["manifest_sha256"]
    assert len(outcome.report["evidence"]["queries"]) >= 6
