"""Comparator 5 of docs/BENCHMARK.md: a named schema-diff tool, labelled schema-only (#6).

    python scripts/benchmark/schema_diff_assessment.py

Runs ``migra`` (the named tool) against the database-tier corpus, entirely in throwaway
containers: one PostgreSQL 17 for the databases and one Python 3.11 image with ``migra`` and
its pinned dependencies (it does not import on Python 3.12, which removed ``pkg_resources``
from its standard environment). Every database holds only the published fixture. Both
containers and their network are removed afterwards.

Two comparisons, fixed before any result was seen:

* **A, raw:** the base schema (reference) against each case's migrated candidate. A schema
  diff reports what a migration changed; every case here adds a ledger.
* **B, against the intended target:** the reviewed safe migration's candidate
  (``scenarios/commerce/changes/ledger-backfill-01``) against each case's candidate: a schema
  review against the schema the pull request meant to produce, the fairest use of the tool.

A case is *flagged* when migra's diff is non-empty. Faults and clean controls are scored from
the label manifest, read only after every case has run. Results go to
``benchmarks/results/schema-diff-results.json`` and ``benchmarks/results/SCHEMA_DIFF.md``.
This is an assessment run by hand, not part of CI: it needs Docker and downloads the tool.
"""

import json
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "benchmarks" / "src"))

from dataproof_benchmark.harness import candidate, databases, migrate  # noqa: E402

CORPUS = REPO / "benchmarks" / "corpus"
SAFE = REPO / "scenarios" / "commerce" / "changes" / "ledger-backfill-01"
RESULTS = REPO / "benchmarks" / "results"
POSTGRES = (
    "postgres:17-alpine@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b"
)
MIGRA = "migra==3.0.1663481299"
REQUIREMENTS = (
    f"{MIGRA}\nschemainspect==3.1.1663587362\nsqlbag==0.1.1617247075\n"
    "psycopg2-binary==2.9.10\nsetuptools==75.8.0\n"
)
DOCKERFILE = """\
FROM python:3.11-slim
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt
ENTRYPOINT ["migra"]
"""


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker binary
        ["docker", *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        check=check,
    )


def build_migra(directory: Path) -> str:
    (directory / "Dockerfile").write_text(DOCKERFILE, "utf-8")
    (directory / "requirements.txt").write_text(REQUIREMENTS, "utf-8")
    iid = directory / "image-id"
    docker("build", "--iidfile", str(iid), str(directory))
    return iid.read_text("utf-8").strip()


def published_port(container: str) -> int:
    out = docker("port", container, "5432/tcp").stdout.strip().splitlines()[0]
    return int(out.rsplit(":", 1)[1])


def dbname(dsn: str) -> str:
    return str(psycopg.conninfo.conninfo_to_dict(dsn)["dbname"])


def ready(dsn: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2):
                return
        except psycopg.OperationalError:
            time.sleep(1)
    raise SystemExit("the throwaway PostgreSQL did not start")


def migra(image: str, network: str, source: str, target: str) -> dict[str, Any]:
    """migra's diff from ``source`` to ``target`` (database names on the throwaway server)."""
    done = docker(
        "run",
        "--rm",
        "--network",
        network,
        image,
        "--unsafe",
        f"postgresql://postgres@pg:5432/{source}",
        f"postgresql://postgres@pg:5432/{target}",
        check=False,
    )
    # migra exits 0 with no differences, 2 with differences, anything else on an error.
    if done.returncode not in (0, 2):
        return {"error": done.stderr.strip()[-500:], "flagged": None, "statements": []}
    statements = [s.strip() for s in done.stdout.split(";") if s.strip()]
    return {"flagged": bool(statements), "statements": statements}


def main() -> int:
    suffix = secrets.token_hex(4)
    network, server = f"dp-migra-{suffix}", f"dp-migra-pg-{suffix}"
    labels = json.loads((CORPUS / "labels.json").read_text("utf-8"))["cases"]
    with tempfile.TemporaryDirectory() as context:
        image = build_migra(Path(context))
    tool_version = docker("run", "--rm", "--entrypoint", "pip", image, "freeze").stdout
    docker("network", "create", network)
    try:
        docker(
            "run", "-d", "--name", server, "--network", network, "--network-alias", "pg",
            "-p", "127.0.0.1::5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", POSTGRES,
        )  # fmt: skip
        admin = f"postgresql://postgres@127.0.0.1:{published_port(server)}/postgres"
        ready(admin)
        with databases(admin) as new_database:
            reference = new_database()
            migrate(reference, None)  # the base schema; a schema diff needs no data here
            reference_name = dbname(reference)
            intended_name = dbname(candidate(new_database, SAFE))
            cases = []
            for label in labels:
                name = dbname(candidate(new_database, CORPUS / "changes" / label["change"]))
                cases.append(
                    {
                        "case_id": label["case_id"],
                        "raw": migra(image, network, reference_name, name),
                        "against_intended": migra(image, network, intended_name, name),
                    }
                )
                print(label["case_id"], "done", flush=True)
    finally:
        docker("rm", "-f", server, check=False)
        docker("network", "rm", network, check=False)
        docker("image", "rm", "-f", image, check=False)
    by_id = {label["case_id"]: label for label in labels}
    for case in cases:
        label = by_id[case["case_id"]]
        case.update(kind=label["kind"], family=label["family"], variant=label["variant"])
    document = {
        "tool": MIGRA,
        "environment": [line for line in tool_version.splitlines() if "==" in line],
        "postgres": POSTGRES,
        "intended_target": "scenarios/commerce/changes/ledger-backfill-01",
        "cases": cases,
    }
    RESULTS.joinpath("schema-diff-results.json").write_text(
        json.dumps(document, indent=2) + "\n", "utf-8"
    )
    RESULTS.joinpath("SCHEMA_DIFF.md").write_text(report(document), "utf-8", newline="\n")
    print("wrote", RESULTS / "SCHEMA_DIFF.md")
    return 0


def _score(cases: list[dict[str, Any]], comparison: str, kind: str) -> str:
    chosen = [c for c in cases if c["kind"] == kind]
    flagged = sum(1 for c in chosen if c[comparison]["flagged"])
    errors = sum(1 for c in chosen if c[comparison]["flagged"] is None)
    text = f"{flagged}/{len(chosen)}"
    return text + (f" ({errors} errors)" if errors else "")


def report(document: dict[str, Any]) -> str:
    cases = document["cases"]
    lines = [
        "# Schema diff (comparator 5): migra, schema-only",
        "",
        "Generated by `scripts/benchmark/schema_diff_assessment.py` from "
        "`schema-diff-results.json`. Do not edit by hand. An assessment run by hand, in "
        "throwaway containers, against the database-tier corpus; not part of CI.",
        "",
        f"- Tool: `{document['tool']}` on Python 3.11, against `{document['postgres']}`.",
        "- **Schema-only:** migra compares catalogs, never values. It is not a value "
        "comparator and is not scored as one.",
        "- A case is *flagged* when migra's diff is non-empty.",
        "",
        "| Comparison | Faults flagged | Clean controls flagged |",
        "| --- | --- | --- |",
        f"| A, raw: base schema to the case's candidate | {_score(cases, 'raw', 'fault')} "
        f"| {_score(cases, 'raw', 'clean')} |",
        "| B, against the intended target: the reviewed safe migration's candidate to the "
        f"case's candidate | {_score(cases, 'against_intended', 'fault')} "
        f"| {_score(cases, 'against_intended', 'clean')} |",
        "",
        "## Per case",
        "",
        "| Case | Kind | Family | Variant | A | B: what differs from the intended schema |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        b = case["against_intended"]
        what = (
            "error"
            if b["flagged"] is None
            else "; ".join(" ".join(s.split())[:90] for s in b["statements"][:2]) or "nothing"
        )
        if b["flagged"] and len(b["statements"]) > 2:
            what += f"; and {len(b['statements']) - 2} more"
        a = case["raw"]["flagged"]
        lines.append(
            f"| `{case['case_id']}` | {case['kind']} | {case['family']} | {case['variant']} "
            f"| {'error' if a is None else 'flagged' if a else 'no diff'} "
            f"| {what.replace('|', '\\|')} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
