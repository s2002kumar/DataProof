import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from conftest import EXAMPLES_DIR, SCHEMAS_DIR
from dataproof_contracts.schema import EXPORTED_MODELS, main, schema_filename, stale_schemas

EXAMPLES = sorted(EXAMPLES_DIR.glob("*.json"))


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS_DIR / schema_filename(name)).read_text("utf-8"))
    return loaded


def _contains_key(node: object, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_contains_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_contains_key(v, key) for v in node)
    return False


def test_committed_schemas_are_current() -> None:
    assert stale_schemas(SCHEMAS_DIR) == [], (
        "run: uv run python -m dataproof_contracts.schema packages/contracts/schemas"
    )


@pytest.mark.parametrize("name", sorted(EXPORTED_MODELS))
def test_schema_is_plain_json_schema_2020_12(name: str) -> None:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert not _contains_key(schema, "discriminator")


def test_every_model_has_an_example_and_every_example_a_model() -> None:
    covered = {path.name.split(".")[0] for path in EXAMPLES}
    assert covered == set(EXPORTED_MODELS)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_round_trips_and_matches_its_schema(path: Path) -> None:
    name = path.name.split(".")[0]
    document = json.loads(path.read_text("utf-8"))
    model = EXPORTED_MODELS[name].model_validate_json(path.read_text("utf-8"))
    assert json.loads(model.model_dump_json()) == document
    validator = Draft202012Validator(
        _schema(name), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    assert list(validator.iter_errors(document)) == []


def test_schema_rejects_float_money() -> None:
    document = json.loads((EXAMPLES_DIR / "CheckResult.f1-detected.json").read_text("utf-8"))
    document["mismatch_examples"][0]["observed"]["value"] = 99.98
    assert not Draft202012Validator(_schema("CheckResult")).is_valid(document)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("49.99", True),
        ("-0", True),
        ("0E-8", True),
        ("1.5e+3", True),
        ("abc", False),
        ("49,99", False),
    ],
)
def test_schema_constrains_decimal_strings(value: str, valid: bool) -> None:
    document = json.loads((EXAMPLES_DIR / "CheckResult.f1-detected.json").read_text("utf-8"))
    document["mismatch_examples"][0]["observed"]["value"] = value
    assert Draft202012Validator(_schema("CheckResult")).is_valid(document) is valid


def test_check_mode_detects_stale_and_extra_schemas(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 0
    assert main([str(tmp_path), "--check"]) == 0
    (tmp_path / schema_filename("CheckResult")).write_text("{}\n", encoding="utf-8")
    (tmp_path / "Retired.schema.json").write_text("{}\n", encoding="utf-8")
    assert stale_schemas(tmp_path) == ["CheckResult.schema.json", "Retired.schema.json"]
    assert main([str(tmp_path), "--check"]) == 1
