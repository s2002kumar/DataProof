"""Load the baseline fixture into a database that is at the reference schema revision."""

from typing import Any

from psycopg import Connection, sql

from commerce_scenario.fixture import Fixture


def load_fixture(connection: Connection[Any], fixture: Fixture) -> None:
    """COPY every table in foreign-key order. The caller owns the transaction."""
    with connection.cursor() as cursor:
        for table, columns, rows in fixture.tables():
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
            )
            with cursor.copy(statement) as copy:
                for row in rows:
                    copy.write_row(row)
