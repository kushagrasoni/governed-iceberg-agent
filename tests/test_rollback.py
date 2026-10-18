import pytest

from governed_agent.executor import RollbackRefused, StaleBaseError
from governed_agent.lakehouse import read, table_exists
from governed_agent.profiler import profile
from conftest import approve, plan_for

TX = "bank.transactions"


def tx(gov):
    return gov.catalog.load_table(TX)


def test_rollback_restores_pre_change_snapshot(gov):
    before_snap, before_prof = tx(gov).current_snapshot().snapshot_id, profile(read(tx(gov)))
    out = approve(gov, plan_for(gov, "remove duplicate transactions"))
    assert out.status == "EXECUTED" and profile(read(tx(gov)))["duplicate_txn_id"] == 0
    gov.rollback(out.plan_id, approver="ops.lead")
    assert tx(gov).current_snapshot().snapshot_id == before_snap
    assert profile(read(tx(gov))) == before_prof
    assert gov.audit.records(out.plan_id)[-1]["event_type"] == "ROLLED_BACK"


def test_rollback_of_add_column_removes_column(gov):
    before = tx(gov).current_snapshot().snapshot_id
    out = approve(gov, plan_for(gov, "add amount band"))
    assert "amount_band" in tx(gov).schema().column_names
    gov.rollback(out.plan_id, approver="ops.lead")
    t = tx(gov)
    assert "amount_band" not in t.schema().column_names and t.current_snapshot().snapshot_id == before


def test_rollback_of_derived_table_drops_it(gov):
    out = approve(gov, plan_for(gov, "daily summary"), auto_approve_low_risk=True)
    assert table_exists(gov.catalog, "analytics.daily_merchant_summary")
    gov.rollback(out.plan_id, approver="ops.lead")
    assert not table_exists(gov.catalog, "analytics.daily_merchant_summary")


def test_rollback_refused_if_table_moved_then_lifo_works(gov):
    first = approve(gov, plan_for(gov, "standardize currency"))
    second = approve(gov, plan_for(gov, "merchant category"))
    with pytest.raises(RollbackRefused):
        gov.rollback(first.plan_id, approver="ops.lead")
    assert any(r["event_type"] == "ROLLBACK_REFUSED" for r in gov.audit.records(first.plan_id))
    gov.rollback(second.plan_id, approver="ops.lead")
    gov.rollback(first.plan_id, approver="ops.lead")
    assert profile(read(tx(gov)))["noncanonical_currency"] > 0


def test_double_rollback_and_unnamed_operator_refused(gov):
    out = approve(gov, plan_for(gov, "standardize currency"))
    with pytest.raises(RollbackRefused):
        gov.rollback(out.plan_id, approver="")
    gov.rollback(out.plan_id, approver="ops.lead")
    with pytest.raises(RollbackRefused):
        gov.rollback(out.plan_id, approver="ops.lead")


def test_force_rollback_when_moved(gov):
    base = tx(gov).current_snapshot().snapshot_id
    first = approve(gov, plan_for(gov, "standardize currency"))
    approve(gov, plan_for(gov, "merchant category"))
    gov.rollback(first.plan_id, approver="ops.lead", force=True)
    assert tx(gov).current_snapshot().snapshot_id == base


def test_stale_base_is_not_published(gov):
    plan, _ = plan_for(gov, "standardize currency")
    dr = gov.executor.dry_run(plan)
    approve(gov, plan_for(gov, "merchant category"))  # main moves underneath
    with pytest.raises(StaleBaseError):
        gov.executor.publish(plan, dr, {"approver": "x"})
