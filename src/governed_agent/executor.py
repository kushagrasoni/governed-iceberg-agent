"""Write-Audit-Publish on Iceberg.

* In-place data changes (FIX_DATA_QUALITY, DEDUPLICATE, DELETE_ROWS, APPEND_RECORDS) are written
  to an Iceberg *branch* `wap_<plan_id>`; main is untouched until approval. Publishing moves main
  to exactly the snapshot that was evaluated (no recompute), then tags it `approved-<plan_id>`.
* Schema changes (ADD_COLUMN) and new tables (CREATE_DERIVED_TABLE) are staged in a separate
  staging table, because an Iceberg schema is table-wide and cannot be isolated on a branch.
* Every publish returns a rollback pointer; `rollback()` restores the pre-change state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.table import Table
from pyiceberg.types import (BooleanType, DateType, DoubleType, IntegerType, LongType, StringType,
                             TimestampType)

from .lakehouse import conform, read, sandboxed_sql, table_exists
from .plan import Plan

BRANCH_OPS = {"FIX_DATA_QUALITY", "DEDUPLICATE", "DELETE_ROWS", "APPEND_RECORDS"}
ARROW_TYPES = {"string": pa.string(), "double": pa.float64(), "long": pa.int64(), "int": pa.int32(),
               "boolean": pa.bool_(), "timestamp": pa.timestamp("us"), "date": pa.date32()}
ICEBERG_TYPES = {"string": StringType, "double": DoubleType, "long": LongType, "int": IntegerType,
                 "boolean": BooleanType, "timestamp": TimestampType, "date": DateType}


class StaleBaseError(RuntimeError):
    """main moved after the dry run; the evaluated snapshot is no longer a fast-forward."""


class RollbackRefused(RuntimeError):
    pass


@dataclass
class DryRun:
    mode: str  # branch | staging_table
    target_table: str
    base_snapshot_id: int | None
    staged_snapshot_id: int | None = None
    branch: str | None = None
    staging_table: str | None = None
    raw_result_schema: pa.Schema | None = None
    pre: pa.Table | None = None
    post: pa.Table | None = None
    error: str | None = None

    def summary(self) -> dict:
        return {"mode": self.mode, "target_table": self.target_table, "base_snapshot_id": self.base_snapshot_id,
                "staged_snapshot_id": self.staged_snapshot_id, "branch": self.branch,
                "staging_table": self.staging_table, "error": self.error,
                "result_columns": self.raw_result_schema.names if self.raw_result_schema is not None else None}


@dataclass
class ExecResult:
    table: str
    pre_snapshot_id: int | None
    post_snapshot_id: int
    rollback_pointer: dict = field(default_factory=dict)


class Executor:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    # ------------------------------------------------------------------ dry run
    def _relations(self, plan: Plan, pre: pa.Table, table: Table) -> dict:
        rel = {"source": pre}
        for name, path in (plan.inputs or {}).items():
            rows = json.loads(Path(path).read_text())
            rel[name] = conform(pa.Table.from_pylist(rows), table.schema().as_arrow()) if rows else table.schema().as_arrow().empty_table()
        return rel

    def dry_run(self, plan: Plan) -> DryRun:
        table = self.catalog.load_table(plan.target_table)
        base = table.current_snapshot().snapshot_id if table.current_snapshot() else None
        pre = read(table)
        mode = "branch" if plan.operation in BRANCH_OPS else "staging_table"
        dr = DryRun(mode=mode, target_table=plan.target_table, base_snapshot_id=base, pre=pre)
        try:
            result = sandboxed_sql(plan.sql, **self._relations(plan, pre, table))
        except Exception as e:
            dr.error = f"transform failed: {type(e).__name__}: {e}"[:800]
            return dr
        dr.raw_result_schema = result.schema
        props = {"gia.plan_id": plan.plan_id, "gia.stage": "dry-run", "gia.operation": plan.operation}
        try:
            if mode == "branch":
                post = conform(result, pre.schema)
                dr.branch = f"wap_{plan.plan_id}".replace("-", "_")
                table.manage_snapshots().create_branch(base, dr.branch).commit()
                table.overwrite(post, branch=dr.branch, snapshot_properties=props)
                table.refresh()
                dr.staged_snapshot_id = table.refs()[dr.branch].snapshot_id
                dr.post = read(table, dr.staged_snapshot_id)
            else:
                if plan.operation == "ADD_COLUMN":
                    fields = list(pre.schema) + [pa.field(c["name"], ARROW_TYPES[c["type"].lower()]) for c in plan.new_columns
                                                 if c["name"] not in pre.schema.names]
                    schema = pa.schema(fields)
                    post = conform(result, schema)
                    ns, name = plan.target_table.split(".")
                else:  # CREATE_DERIVED_TABLE
                    post = result
                    schema = result.schema
                    ns, name = plan.output_table.split(".")
                dr.staging_table = f"{ns}.{name}__wap_{plan.plan_id.replace('-', '_')}"
                st = self.catalog.create_table(dr.staging_table, schema=schema,
                                               properties={"gia.staging": "true", "gia.plan_id": plan.plan_id})
                st.append(post, snapshot_properties=props)
                dr.staged_snapshot_id = st.current_snapshot().snapshot_id
                dr.post = read(st)
        except Exception as e:
            dr.error = f"staging write failed: {type(e).__name__}: {e}"[:800]
        return dr

    def discard(self, dr: DryRun) -> None:
        """Throw away an unapproved change. Main was never touched."""
        if dr.branch:
            table = self.catalog.load_table(dr.target_table)
            if dr.branch in table.refs():
                table.manage_snapshots().remove_branch(dr.branch).commit()
        if dr.staging_table and table_exists(self.catalog, dr.staging_table):
            self.catalog.drop_table(dr.staging_table)

    # ------------------------------------------------------------------ publish
    def publish(self, plan: Plan, dr: DryRun, approval: dict) -> ExecResult:
        tag = f"approved-{plan.plan_id}"
        if dr.mode == "branch":
            table = self.catalog.load_table(dr.target_table)
            cur = table.current_snapshot().snapshot_id if table.current_snapshot() else None
            if cur != dr.base_snapshot_id:
                raise StaleBaseError(f"{dr.target_table} moved from {dr.base_snapshot_id} to {cur} since the dry run; re-plan")
            (table.manage_snapshots()
                  .set_current_snapshot(dr.staged_snapshot_id)
                  .remove_branch(dr.branch)
                  .create_tag(dr.staged_snapshot_id, tag)
                  .commit())
            return ExecResult(dr.target_table, dr.base_snapshot_id, dr.staged_snapshot_id,
                              {"kind": "restore_snapshot", "table": dr.target_table,
                               "restore_snapshot_id": dr.base_snapshot_id,
                               "expected_current_snapshot_id": dr.staged_snapshot_id})

        if plan.operation == "ADD_COLUMN":
            table = self.catalog.load_table(dr.target_table)
            cur = table.current_snapshot().snapshot_id if table.current_snapshot() else None
            if cur != dr.base_snapshot_id:
                raise StaleBaseError(f"{dr.target_table} moved since the dry run; re-plan")
            new_cols = [c for c in plan.new_columns if c["name"] not in table.schema().column_names]
            props = {"gia.plan_id": plan.plan_id, "gia.stage": "publish", "gia.approver": approval.get("approver", "")}
            with table.transaction() as tx:
                with tx.update_schema() as us:
                    for c in new_cols:
                        us.add_column(c["name"], ICEBERG_TYPES[c["type"].lower()](), doc=c.get("doc"))
                tx.overwrite(dr.post, snapshot_properties=props)
            table.refresh()
            post_id = table.current_snapshot().snapshot_id
            table.manage_snapshots().create_tag(post_id, tag).commit()
            self.catalog.drop_table(dr.staging_table)
            return ExecResult(dr.target_table, dr.base_snapshot_id, post_id,
                              {"kind": "restore_snapshot", "table": dr.target_table,
                               "restore_snapshot_id": dr.base_snapshot_id,
                               "expected_current_snapshot_id": post_id,
                               "drop_columns": [c["name"] for c in new_cols]})

        # CREATE_DERIVED_TABLE: promote the evaluated staging table by renaming it
        self.catalog.rename_table(dr.staging_table, plan.output_table)
        out = self.catalog.load_table(plan.output_table)
        with out.transaction() as tx:
            tx.set_properties({"gia.staging": "false", "gia.created_by_plan": plan.plan_id,
                               "gia.approver": approval.get("approver", "")})
        out.refresh()
        out.manage_snapshots().create_tag(out.current_snapshot().snapshot_id, tag).commit()
        return ExecResult(plan.output_table, None, out.current_snapshot().snapshot_id,
                          {"kind": "drop_created_table", "table": plan.output_table,
                           "table_uuid": str(out.metadata.table_uuid)})

    # ------------------------------------------------------------------ rollback
    def rollback(self, pointer: dict, force: bool = False) -> dict:
        kind = pointer["kind"]
        if kind == "restore_snapshot":
            table = self.catalog.load_table(pointer["table"])
            cur = table.current_snapshot().snapshot_id
            if cur != pointer["expected_current_snapshot_id"] and not force:
                raise RollbackRefused(
                    f"{pointer['table']} has moved on (current={cur}, expected={pointer['expected_current_snapshot_id']}); "
                    "rolling back would also discard later changes. Re-run with --force after review.")
            table.manage_snapshots().set_current_snapshot(pointer["restore_snapshot_id"]).commit()
            dropped = []
            if pointer.get("drop_columns"):
                table.refresh()
                with table.update_schema() as us:
                    for c in pointer["drop_columns"]:
                        if c in table.schema().column_names:
                            us.delete_column(c)
                            dropped.append(c)
            table.refresh()
            return {"table": pointer["table"], "from_snapshot_id": cur,
                    "restored_snapshot_id": table.current_snapshot().snapshot_id, "dropped_columns": dropped}
        if kind == "drop_created_table":
            table = self.catalog.load_table(pointer["table"])
            if str(table.metadata.table_uuid) != pointer["table_uuid"]:
                raise RollbackRefused(f"{pointer['table']} is not the table this plan created (uuid mismatch)")
            # drop from the catalog but keep data files on disk as evidence (no purge)
            self.catalog.drop_table(pointer["table"])
            return {"table": pointer["table"], "dropped": True, "data_files_retained": True}
        raise RollbackRefused(f"unknown rollback pointer kind {kind}")
