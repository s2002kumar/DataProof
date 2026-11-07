"""Step variables: what a trusted configuration may add to a migration step's environment.

The rules are checked twice, where the variables are declared (``dataproof_runner.backend``)
and where they are applied (the trusted entrypoint, which may import only the standard library),
so both are held to the same cases here.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from dataproof_action import ConfigError, parse_config
from dataproof_action.migrator import merged_requirements, requirement_names
from dataproof_runner.backend import resolve_step_variables, step_variable_problems
from subscriptions_env import EXAMPLE

ENTRYPOINT = Path(__file__).resolve().parents[2] / "infra" / "docker" / "runtime" / "entrypoint.py"
COORDINATES = {
    "host": "postgres",
    "port": "5432",
    "user": "dataproof_owner",
    "password": "s3cret",
    "dbname": "app",
    "database_url": "postgresql+psycopg://dataproof_owner:s3cret@postgres:5432/app",
}


@pytest.fixture(scope="module")
def entrypoint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dataproof_entrypoint", ENTRYPOINT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_placeholders_name_the_steps_own_database() -> None:
    variables = {
        "POSTGRES_SERVER": "{host}",
        "POSTGRES_PORT": "{port}",
        "POSTGRES_USER": "{user}",
        "POSTGRES_PASSWORD": "{password}",
        "POSTGRES_DB": "{dbname}",
        "APP_DATABASE_URL": "{database_url}",
        "SECRET_KEY": "dataproof-test-only",
        "PYTHONPATH": ".",
    }
    assert step_variable_problems(variables) == []
    resolved = resolve_step_variables(variables, COORDINATES)
    assert resolved["POSTGRES_SERVER"] == "postgres"
    assert resolved["POSTGRES_PASSWORD"] == "s3cret"  # noqa: S105 - a test value
    assert resolved["APP_DATABASE_URL"] == COORDINATES["database_url"]
    assert resolved["SECRET_KEY"] == "dataproof-test-only"  # noqa: S105 - a test value


@pytest.mark.parametrize(
    ("variables", "problem"),
    [
        ({"DATABASE_URL": "x"}, "set by DataProof"),
        ({"PATH": "/tmp"}, "set by DataProof"),  # noqa: S108 - a test value
        ({"HOME": "/"}, "set by DataProof"),
        ({"DATAPROOF_SOURCE_SHA256": "0" * 64}, "set by DataProof"),
        ({"lower": "x"}, "not an upper-case variable name"),
        ({"A-B": "x"}, "not an upper-case variable name"),
        ({"URL": "{env.SECRET}"}, "the placeholders are"),
        ({"URL": "{password"}, "unmatched brace"),
        ({"BIG": "x" * 1025}, "longer than"),
        ({f"V{i}": "x" for i in range(33)}, "at most 32"),
    ],
)
def test_unacceptable_variables_are_refused(variables: dict[str, str], problem: str) -> None:
    assert any(problem in p for p in step_variable_problems(variables))
    with pytest.raises(ValueError, match=problem):
        resolve_step_variables(variables, COORDINATES)


def test_the_entrypoint_checks_the_same_rules_again(entrypoint: ModuleType) -> None:
    accepted = entrypoint.step_variables(json.dumps({"POSTGRES_USER": "u", "PYTHONPATH": "."}))
    assert accepted == {"POSTGRES_USER": "u", "PYTHONPATH": "."}
    assert entrypoint.step_variables(None) == {}
    assert entrypoint.step_variables("") == {}
    for raw in (
        json.dumps({"DATABASE_URL": "postgresql://elsewhere"}),
        json.dumps({"PATH": "/tmp/evil"}),  # noqa: S108 - a test value
        json.dumps({"DATAPROOF_X": "1"}),
        json.dumps({"lower": "1"}),
        json.dumps({"A": 1}),
        json.dumps({"A": "x\0y"}),
        json.dumps(["A", "B"]),
        "{not json",
    ):
        assert entrypoint.step_variables(raw) is None, raw


def test_the_configuration_declares_variables_on_the_base_branch_only() -> None:
    text = (
        (EXAMPLE / "dataproof" / "config.toml")
        .read_text("utf-8")
        .replace(
            "timeout_seconds = 300",
            'timeout_seconds = 300\nenvironment = { POSTGRES_PASSWORD = "{password}", '
            'SECRET_KEY = "test-only" }',
        )
    )
    config = parse_config(text)
    assert config.alembic.environment == {
        "POSTGRES_PASSWORD": "{password}",
        "SECRET_KEY": "test-only",
    }
    with pytest.raises(ConfigError, match="set by DataProof"):
        parse_config(text.replace("SECRET_KEY", "DATABASE_URL"))
    with pytest.raises(ConfigError, match="the placeholders are"):
        parse_config(text.replace("{password}", "{env}"))


def test_requirements_that_name_a_package_replace_its_default_pin() -> None:
    assert requirement_names("SQLModel==0.0.21  # the app\nemail_validator>=2\n-r other.txt\n") == {
        "sqlmodel",
        "email-validator",
    }
    default = merged_requirements(None)
    assert default.splitlines() == [
        "alembic==1.20.1",
        "sqlalchemy==2.1.6",
        "psycopg[binary]==3.3.6",
    ]
    replaced = merged_requirements("SQLAlchemy==2.0.36\nsqlmodel==0.0.21\n").splitlines()
    assert "sqlalchemy==2.1.6" not in replaced
    assert replaced == [
        "alembic==1.20.1",
        "psycopg[binary]==3.3.6",
        "SQLAlchemy==2.0.36",
        "sqlmodel==0.0.21",
    ]
