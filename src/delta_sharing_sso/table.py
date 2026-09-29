"""Table: a handle to one Delta Sharing table, so you stop repeating share/schema/table everywhere.

    t = catalog.table("sales.orders")     # or catalog.sales.orders, if "orders" is unique in "sales"
    t.columns                             # no data downloaded
    t.to_pandas(limit=1000)
    t.where(country="DE").to_pandas()
    t.between("event_date", "2026-05-01", "2026-05-31").to_pandas()

Table handles are lazy and cheap to create: nothing is fetched over the network until you read
.columns, .version, or call .to_pandas() / .load_changes().
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .predicates import DELTA_TO_VALUE_TYPE, and_
from .predicates import between as _between
from .predicates import equal as _equal


class Table:
    """A handle to one share.schema.table."""

    def __init__(self, client: Any, share: str, schema: str, table: str,
                 _predicate: dict[str, Any] | None = None):
        self._client = client
        self.share = share
        self.schema = schema
        self.table = table
        self._predicate = _predicate
        self._columns_cache: pd.DataFrame | None = None
        self._version_cache: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.share}.{self.schema}.{self.table}"

    @property
    def columns(self) -> pd.DataFrame:
        """Column names, types and partition flags. Cached; call refresh_columns() to reload."""
        if self._columns_cache is None:
            self._columns_cache = self._client.get_table_columns(self.share, self.schema, self.table)
        return self._columns_cache

    def refresh_columns(self) -> pd.DataFrame:
        self._columns_cache = None
        return self.columns

    @property
    def version(self) -> int:
        if self._version_cache is None:
            self._version_cache = self._client.get_table_version(self.share, self.schema, self.table)
        return self._version_cache

    def to_pandas(self, limit: int | None = 5, version: int | None = None) -> pd.DataFrame:
        """Load this table (with any .where()/.between() filters applied) as a pandas DataFrame."""
        return self._client.load_table_as_dataframe(
            self.share, self.schema, self.table, limit=limit,
            json_predicate_hints=self._predicate, version=version)

    def load_changes(self, **kwargs) -> pd.DataFrame:
        """Rows changed between versions/timestamps. Requires change data feed on the shared table."""
        return self._client.load_table_changes_as_dataframe(self.share, self.schema, self.table, **kwargs)

    def _value_type(self, column: str) -> str:
        row = self.columns.loc[self.columns["name"] == column]
        return DELTA_TO_VALUE_TYPE.get(row.iloc[0]["type"], "string") if len(row) else "string"

    def _filtered(self, predicate: dict[str, Any]) -> "Table":
        combined = and_(self._predicate, predicate) if self._predicate else predicate
        return Table(self._client, self.share, self.schema, self.table, _predicate=combined)

    def where(self, **equals: Any) -> "Table":
        """Return a copy of this table filtered to column == value for every keyword given.

            catalog.table("sales.orders").where(country="DE").to_pandas()

        Column types (for the predicate's valueType) are looked up from the table's metadata.
        """
        preds = [_equal(col, val, self._value_type(col)) for col, val in equals.items()]
        combined = preds[0] if len(preds) == 1 else and_(*preds)
        return self._filtered(combined)

    def between(self, column: str, low: Any, high: Any, value_type: str | None = None) -> "Table":
        """Return a copy of this table filtered to low <= column <= high (inclusive)."""
        return self._filtered(_between(column, low, high, value_type or self._value_type(column)))

    def __repr__(self) -> str:
        return f"<Table {self.full_name}>"

    def _repr_html_(self) -> str:
        """Rich display in Jupyter: columns and a small preview, without calling .df() yourself first."""
        try:
            cols, preview = self.columns, self.to_pandas(limit=5)
            return (
                f"<b>{self.full_name}</b> (version {self.version}) &mdash; {len(cols)} columns<br>"
                + cols.to_html(index=False, max_rows=20)
                + "<br><i>preview (5 rows):</i>"
                + preview.to_html(index=False)
            )
        except Exception as exc:
            return f"<b>{self.full_name}</b> &mdash; preview unavailable: {exc}"


class _ShareView:
    """catalog.<share> -> a table directly if its name is unique in this share, else catalog.<share>.<schema>."""

    def __init__(self, client: Any, share: str):
        self._client = client
        self._share = share

    def _tables(self) -> list[tuple[str, str]]:
        return [(sc, t) for s, sc, t in self._client._browse_names() if s == self._share]

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        matches = self._tables()
        if name in {sc for sc, _ in matches}:
            return _SchemaView(self._client, self._share, name)
        table_matches = [(sc, t) for sc, t in matches if t == name]
        if len(table_matches) == 1:
            schema, table = table_matches[0]
            return Table(self._client, self._share, schema, table)
        if len(table_matches) > 1:
            schemas = sorted(sc for sc, _ in table_matches)
            raise AttributeError(
                f"{name!r} exists in multiple schemas under share {self._share!r}: {schemas}. "
                f"Use catalog.table('{self._share}.<schema>.{name}') instead.")
        raise AttributeError(name)

    def __getitem__(self, name: str):
        return getattr(self, name)

    def __dir__(self):
        matches = self._tables()
        return sorted({sc for sc, _ in matches} | {t for _, t in matches})

    def __repr__(self):
        matches = self._tables()
        n_schemas = len({sc for sc, _ in matches})
        return f"<Share {self._share!r}: {n_schemas} schema(s), {len(matches)} table(s)>"


class _SchemaView:
    """catalog.<share>.<schema> -> catalog.<share>.<schema>.<table>."""

    def __init__(self, client: Any, share: str, schema: str):
        self._client = client
        self._share = share
        self._schema = schema

    def _tables(self) -> list[str]:
        return [t for s, sc, t in self._client._browse_names() if s == self._share and sc == self._schema]

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._tables():
            return Table(self._client, self._share, self._schema, name)
        raise AttributeError(name)

    def __getitem__(self, name: str):
        return getattr(self, name)

    def __dir__(self):
        return sorted(self._tables())

    def __repr__(self):
        return f"<Schema {self._share}.{self._schema}: {len(self._tables())} table(s)>"
