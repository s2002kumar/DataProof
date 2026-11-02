"""FK-closure subsets of a baseline database, for reproductions (VERIFICATION tier 6).

The rule is fixed, so that a reproduction never widens its subset until a failure recurs (#8):

1. **Seeds:** the rows of the seed table (the relation that holds all of the oracle's key
   columns, with its primary key among them) for the failing keys.
2. **Aggregates:** the rows that reference a seed, or reference a seed's direct parent. These
   are the seeds' siblings in the same aggregate, for example an order's items and its other
   payment events.
3. **References:** every row any of those rows references, transitively, so the subset is
   referentially complete.

Everything here is trusted code on a trusted connection: the queries are built from catalog
names with ``psycopg.sql`` and the values are literals or parameters, never text from a PR.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

Row = tuple[Any, ...]


@dataclass(frozen=True)
class ForeignKey:
    child: str
    child_columns: tuple[str, ...]
    parent: str
    parent_columns: tuple[str, ...]


_FOREIGN_KEYS = """
SELECT child.relname, parent.relname,
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY AS k(n, i)
             JOIN pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.i),
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY AS k(n, i)
             JOIN pg_attribute AS a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.i)
FROM pg_constraint AS con
JOIN pg_class AS child ON child.oid = con.conrelid
JOIN pg_class AS parent ON parent.oid = con.confrelid
JOIN pg_namespace AS ns ON ns.oid = child.relnamespace
WHERE con.contype = 'f' AND ns.nspname = 'public'
ORDER BY child.relname, con.conname
"""
_PRIMARY_KEYS = """
SELECT cls.relname,
       ARRAY(SELECT a.attname FROM unnest(idx.indkey) WITH ORDINALITY AS k(n, i)
             JOIN pg_attribute AS a ON a.attrelid = idx.indrelid AND a.attnum = k.n ORDER BY k.i)
FROM pg_index AS idx
JOIN pg_class AS cls ON cls.oid = idx.indrelid
JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
WHERE idx.indisprimary AND ns.nspname = 'public'
"""
_COLUMNS = """
SELECT table_name, array_agg(column_name::text ORDER BY ordinal_position)
FROM information_schema.columns
WHERE table_schema = 'public'
GROUP BY table_name
"""


@dataclass(frozen=True)
class Catalog:
    foreign_keys: tuple[ForeignKey, ...]
    primary_keys: Mapping[str, tuple[str, ...]]
    columns: Mapping[str, tuple[str, ...]]

    @classmethod
    def read(cls, conn: psycopg.Connection[Any]) -> "Catalog":
        return cls(
            foreign_keys=tuple(
                ForeignKey(child, tuple(cc), parent, tuple(pc))
                for child, parent, cc, pc in conn.execute(_FOREIGN_KEYS).fetchall()
            ),
            primary_keys={t: tuple(c) for t, c in conn.execute(_PRIMARY_KEYS).fetchall()},
            columns={t: tuple(c) for t, c in conn.execute(_COLUMNS).fetchall()},
        )

    def seed_table(self, key_columns: Sequence[str]) -> str | None:
        """The table that holds every key column and whose primary key is among them."""
        candidates = [
            table
            for table, columns in self.columns.items()
            if set(key_columns) <= set(columns)
            and table in self.primary_keys
            and set(self.primary_keys[table]) <= set(key_columns)
        ]
        return min(candidates, key=lambda t: (len(self.columns[t]), t)) if candidates else None

    def load_order(self, tables: Iterable[str]) -> list[str]:
        """Parents before children (foreign keys within ``tables``)."""
        remaining = set(tables)
        ordered: list[str] = []
        while remaining:
            ready = sorted(
                t
                for t in remaining
                if not any(
                    fk.child == t and fk.parent in remaining and fk.parent != t
                    for fk in self.foreign_keys
                )
            )
            if not ready:
                raise ValueError(f"foreign keys form a cycle among {sorted(remaining)}")
            ordered += ready
            remaining -= set(ready)
        return ordered


def _where_in(columns: Sequence[str], values: Sequence[Row]) -> sql.Composed:
    row = sql.SQL("({})").format(sql.SQL(", ").join(map(sql.Identifier, columns)))
    literals = sql.SQL(", ").join(
        sql.SQL("({})").format(sql.SQL(", ").join(map(sql.Literal, value))) for value in values
    )
    return sql.SQL("{} IN ({})").format(row, literals)


def _select(
    conn: psycopg.Connection[Any],
    table: str,
    columns: Sequence[str],
    where: Sequence[str],
    values: Iterable[Row],
) -> set[Row]:
    values = sorted(set(values), key=repr)
    if not values:
        return set()
    query = sql.SQL("SELECT {} FROM {} WHERE {}").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.Identifier(table),
        _where_in(where, values),
    )
    return {tuple(row) for row in conn.execute(query).fetchall()}


def closure(
    conn: psycopg.Connection[Any],
    catalog: Catalog,
    seed_table: str,
    key_columns: Sequence[str],
    seed_keys: Sequence[Row],
) -> dict[str, set[Row]]:
    """Primary keys of every row in the subset, by table (see the module docstring)."""
    pk = catalog.primary_keys
    rows: dict[str, set[Row]] = defaultdict(set)
    rows[seed_table] = _select(conn, seed_table, pk[seed_table], key_columns, seed_keys)

    def referenced_by(source: Mapping[str, set[Row]]) -> dict[str, set[Row]]:
        found: dict[str, set[Row]] = defaultdict(set)
        for fk in catalog.foreign_keys:
            keys = source.get(fk.child)
            if not keys:
                continue
            refs = _select(conn, fk.child, fk.child_columns, pk[fk.child], keys)
            found[fk.parent] |= _select(conn, fk.parent, pk[fk.parent], fk.parent_columns, refs)
        return found

    def referencing(source: Mapping[str, set[Row]]) -> dict[str, set[Row]]:
        found: dict[str, set[Row]] = defaultdict(set)
        for fk in catalog.foreign_keys:
            keys = source.get(fk.parent)
            if not keys:
                continue
            targets = _select(conn, fk.parent, fk.parent_columns, pk[fk.parent], keys)
            found[fk.child] |= _select(conn, fk.child, pk[fk.child], fk.child_columns, targets)
        return found

    seeds = dict(rows)
    parents = referenced_by(seeds)
    level = defaultdict(set, {t: set(r) for t, r in seeds.items()})
    for table, keys in parents.items():
        level[table] |= keys
    for table, keys in referencing(level).items():
        rows[table] |= keys
    for table, keys in parents.items():
        rows[table] |= keys
    frontier: dict[str, set[Row]] = dict(rows)
    while frontier:
        added: dict[str, set[Row]] = {}
        for table, keys in referenced_by(frontier).items():
            new = keys - rows[table]
            if new:
                rows[table] |= new
                added[table] = new
        frontier = added
    return {t: r for t, r in rows.items() if r}


def export(
    conn: psycopg.Connection[Any], catalog: Catalog, subset: Mapping[str, set[Row]]
) -> dict[str, str]:
    """The subset's rows as ``COPY`` text per table, in load order."""
    data: dict[str, str] = {}
    for table in catalog.load_order(subset):
        query = sql.SQL("COPY (SELECT * FROM {} WHERE {} ORDER BY {}) TO STDOUT").format(
            sql.Identifier(table),
            _where_in(catalog.primary_keys[table], sorted(subset[table], key=repr)),
            sql.SQL(", ").join(map(sql.Identifier, catalog.primary_keys[table])),
        )
        chunks = []
        with conn.cursor().copy(query) as copy:
            for chunk in copy:
                chunks.append(bytes(chunk))
        data[table] = b"".join(chunks).decode("utf-8")
    return data


def load(conn: psycopg.Connection[Any], data: Mapping[str, str]) -> None:
    """Load an exported subset (tables already in load order) into empty tables."""
    with conn.transaction():
        for table, text in data.items():
            with conn.cursor().copy(
                sql.SQL("COPY {} FROM STDIN").format(sql.Identifier(table))
            ) as copy:
                copy.write(text.encode("utf-8"))


def digest(data: Mapping[str, str]) -> str:
    sha = hashlib.sha256()
    for table, text in data.items():
        sha.update(f"{table}\n".encode())
        sha.update(text.encode("utf-8"))
    return sha.hexdigest()
