"""JSON Schema export for the public contract models.

The committed schemas in ``packages/contracts/schemas`` describe the JSON that DataProof emits
(serialization mode: decimals are strings). Regenerate them with::

    uv run python -m dataproof_contracts.schema packages/contracts/schemas

and verify they are current with ``--check``.
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from dataproof_contracts.check import CheckResult
from dataproof_contracts.contract import DataContract
from dataproof_contracts.decision import HumanDecision
from dataproof_contracts.evidence import EvidenceManifest
from dataproof_contracts.finding import Finding
from dataproof_contracts.run import ReviewRun

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

EXPORTED_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "CheckResult": CheckResult,
        "DataContract": DataContract,
        "EvidenceManifest": EvidenceManifest,
        "Finding": Finding,
        "HumanDecision": HumanDecision,
        "ReviewRun": ReviewRun,
    }
)


def _without_openapi_keywords(node: Any) -> Any:
    """Drop OpenAPI's ``discriminator`` so the output is plain JSON Schema 2020-12.

    The ``oneOf`` it annotates already pins each branch with a ``const`` type tag, so validation
    is unchanged; strict validators such as Ajv reject the unknown keyword.
    """
    if isinstance(node, dict):
        return {k: _without_openapi_keywords(v) for k, v in node.items() if k != "discriminator"}
    if isinstance(node, list):
        return [_without_openapi_keywords(item) for item in node]
    return node


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = _without_openapi_keywords(model.model_json_schema(mode="serialization"))
    return {"$schema": JSON_SCHEMA_DIALECT, **schema}


def render(model: type[BaseModel]) -> str:
    return json.dumps(json_schema(model), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def schema_filename(name: str) -> str:
    return f"{name}.schema.json"


def stale_schemas(directory: Path) -> list[str]:
    """File names that are missing, out of date, or no longer exported."""
    stale = []
    for name, model in EXPORTED_MODELS.items():
        path = directory / schema_filename(name)
        if not path.is_file() or path.read_text(encoding="utf-8") != render(model):
            stale.append(path.name)
    expected = {schema_filename(name) for name in EXPORTED_MODELS}
    stale.extend(sorted(p.name for p in directory.glob("*.schema.json") if p.name not in expected))
    return stale


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, model in EXPORTED_MODELS.items():
        (directory / schema_filename(name)).write_text(
            render(model), encoding="utf-8", newline="\n"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--check", action="store_true", help="fail if schemas are stale")
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_schemas(args.directory)
        if stale:
            print(f"stale JSON schemas: {', '.join(stale)}", file=sys.stderr)
            return 1
        return 0
    write_schemas(args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
