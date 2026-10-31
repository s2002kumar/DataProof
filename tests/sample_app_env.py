"""Versions of the sample app's API, loaded in-process for tests.

The app uses relative imports, so a version can be imported from any directory under a unique
package name: the base app as tracked here, or a copy with the ledger overlay that the ledger
change sets' pull requests apply. Each test client reads its own database.
"""

import importlib
import importlib.util
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parents[1]
BASE_PACKAGE = REPO / "sample-app" / "app"
LEDGER_PAYMENTS = REPO / "scenarios" / "commerce" / "ledger-app" / "payments.py"


def ledger_package(directory: Path) -> Path:
    """A copy of the app with the ledger version of ``payments.py``, as a ledger change has."""
    target = directory / "app"
    shutil.copytree(BASE_PACKAGE, target, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(LEDGER_PAYMENTS, target / "payments.py")
    return target


def import_app(package: Path, name: str) -> tuple[ModuleType, ModuleType]:
    spec = importlib.util.spec_from_file_location(
        name, package / "__init__.py", submodule_search_locations=[str(package)]
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{name}.main"), importlib.import_module(f"{name}.db")


def api_client(package: Path, name: str, dsn: str) -> TestClient:
    """A test client for the app in ``package``, reading the database at ``dsn``."""
    main, db = import_app(package, name)
    application = main.create_app()

    def connection() -> Iterator[Any]:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.read_only = True
            yield conn

    application.dependency_overrides[db.connection] = connection
    return TestClient(application)
