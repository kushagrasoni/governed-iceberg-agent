"""Thin helpers over PyIceberg + DuckDB: create/load tables, read snapshots, run sandboxed SQL."""
from __future__ import annotations

import duckdb
import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.table import Table

from . import datagen

TRANSACTIONS = "bank.transactions"
AUDIT_TABLE = "governance.agent_audit"


def ensure_namespace(catalog: Catalog, ns: str) -> None:
    try:
        catalog.create_namespace(ns)
    except NamespaceAlreadyExistsError:
        pass


def table_exists(catalog: Catalog, ident: str) -> bool:
    try:
        catalog.load_table(ident)
        return True
    except NoSuchTableError:
        return False


def seed_transactions(catalog: Catalog, rows: int = 5000, seed: int = 42, force: bool = False) -> dict:
    ensure_namespace(catalog, "bank")
    if table_exists(catalog, TRANSACTIONS):
        if not force:
            raise RuntimeError(f"{TRANSACTIONS} already exists; use --force to recreate")
        catalog.purge_table(TRANSACTIONS)
    data, manifest = datagen.generate(rows=rows, seed=seed)
    tbl = catalog.create_table(
        TRANSACTIONS,
        schema=datagen.SCHEMA,
        properties={"gia.synthetic": "true", "gia.as_of": datagen.AS_OF.isoformat()},
    )
    tbl.append(data, snapshot_properties={"gia.event": "seed", "gia.synthetic": "true"})
    manifest["snapshot_id"] = tbl.current_snapshot().snapshot_id
    return manifest


def read(table: Table, snapshot_id: int | None = None) -> pa.Table:
    if snapshot_id is None:
        snap = table.current_snapshot()
        if snap is None:
            return table.schema().as_arrow().empty_table()
        snapshot_id = snap.snapshot_id
    return table.scan(snapshot_id=snapshot_id).to_arrow()


def sandboxed_sql(sql: str, **relations: pa.Table) -> pa.Table:
    """Run a SELECT in an in-memory DuckDB with file/network access disabled.

    The only relations visible are the Arrow tables passed in (e.g. source=...).
    """
    con = duckdb.connect(config={"enable_external_access": False})
    try:
        for name, tbl in relations.items():
            con.register(name, tbl)
        con.execute("SET lock_configuration = true")
        return con.execute(sql).to_arrow_table()
    finally:
        con.close()


def conform(result: pa.Table, schema: pa.Schema) -> pa.Table:
    """Reorder/cast a result to an Arrow schema (columns must exist)."""
    cols = [result.column(f.name).cast(f.type) for f in schema]
    return pa.Table.from_arrays(cols, schema=schema)
