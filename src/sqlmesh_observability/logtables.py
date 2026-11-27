"""The package's own tables, reached through an engine adapter with names fixed for its dialect.

SQLMesh's engine adapters quote identifiers when they create, insert, or delete, but not when
they fetch. On a case-sensitive engine such as Snowflake, a table created as ``"invocations"`` is
then not found by ``SELECT ... FROM invocations``, which resolves to ``INVOCATIONS``. Every name
in the package's statements is normalized for the dialect first (upper case on Snowflake), so
quoted and unquoted SQL reach the same tables. Users can query them without quotes, too.

Only the package's tables go through here. Monitors query model tables with the names SQLMesh
and the warehouse report, which are already in the right case.
"""

from __future__ import annotations

import typing as t

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter import EngineAdapter

E = t.TypeVar("E", bound=exp.Expr)


class LogTables:
    """The subset of :class:`EngineAdapter` the package uses on its own tables."""

    def __init__(self, adapter: "EngineAdapter") -> None:
        self.adapter = adapter
        self.dialect = adapter.dialect

    def normalize(self, expression: E) -> E:
        return t.cast(E, normalize_identifiers(expression.copy(), dialect=self.dialect))

    def table(self, schema: str, name: str) -> exp.Table:
        return self.normalize(exp.to_table(f"{schema}.{name}", dialect=self.dialect))

    def columns(self, columns: t.Mapping[str, exp.DataType]) -> t.Dict[str, exp.DataType]:
        return {
            self.normalize(exp.to_identifier(name)).name: kind for name, kind in columns.items()
        }

    # --- Adapter calls ---------------------------------------------------------------------

    def create_schema(self, schema: str) -> None:
        table = self.table(schema, "_")
        self.adapter.create_schema(
            exp.Table(db=table.args.get("db"), catalog=table.args.get("catalog"))
        )

    def create_table(self, table: exp.Table, columns: t.Mapping[str, exp.DataType]) -> None:
        self.adapter.create_table(self.normalize(table), self.columns(columns), exists=True)

    def table_exists(self, table: exp.Table) -> bool:
        return self.adapter.table_exists(self.normalize(table))

    def fetchone(self, query: exp.Expr) -> t.Optional[t.Tuple[t.Any, ...]]:
        return self.adapter.fetchone(self.normalize(query))

    def fetchall(self, query: exp.Expr) -> t.List[t.Tuple[t.Any, ...]]:
        return self.adapter.fetchall(self.normalize(query))

    def delete_from(self, table: exp.Table, where: exp.Expr) -> None:
        self.adapter.delete_from(self.normalize(table), self.normalize(where))

    def insert_append(
        self, table: exp.Table, query: exp.Query, columns: t.Mapping[str, exp.DataType]
    ) -> None:
        self.adapter.insert_append(
            self.normalize(table),
            self.normalize(query),
            target_columns_to_types=self.columns(columns),
        )

    def transaction(self) -> t.ContextManager[None]:
        return self.adapter.transaction()

    def close(self) -> None:
        self.adapter.close()
