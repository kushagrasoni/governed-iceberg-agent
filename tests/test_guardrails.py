from governed_agent.plan import Plan
from governed_agent.policy import check_sql, static_check
from conftest import approve, plan_for

TX = "bank.transactions"


def mk(**kw):
    base = dict(operation="FIX_DATA_QUALITY", target_table=TX, sql="SELECT * FROM source", rationale="t", risk_tier="LOW")
    base.update(kw)
    return Plan(**base)


def failed(res):
    return {c.name for c in res.checks if not c.passed}


def test_unknown_operation_denied(gov):
    r = static_check(mk(operation="GRANT_ACCESS"), gov.policy, gov.catalog)
    assert r.decision == "DENY" and "operation_allowlisted" in failed(r)


def test_irreversible_operation_denied(gov):
    r = static_check(mk(operation="DROP_TABLE"), gov.policy, gov.catalog)
    assert r.decision == "DENY"
    assert "irreversible" in [c for c in r.checks if c.name == "operation_allowlisted"][0].detail


def test_sql_ddl_dml_rejected():
    assert any(not c.passed for c in check_sql("DELETE FROM source WHERE amount < 0"))
    assert any(not c.passed for c in check_sql("SELECT * FROM source; DROP TABLE source"))
    assert any(not c.passed for c in check_sql("SELECT * FROM source -- harmless\n; UPDATE source SET a=1"))


def test_keywords_inside_string_literals_are_fine():
    assert all(c.passed for c in check_sql("SELECT * REPLACE (coalesce(status, 'DELETE ME') AS status) FROM source"))


def test_delete_requires_elevated_tier(gov):
    r = static_check(mk(operation="DELETE_ROWS", sql="SELECT * FROM source WHERE NOT (amount > 1e9)"), gov.policy, gov.catalog)
    assert r.allowed and r.risk_tier == "HIGH" and r.required_role == "elevated"


def test_planner_cannot_lower_risk_tier(gov):
    r = static_check(mk(risk_tier="LOW"), gov.policy, gov.catalog)
    assert r.risk_tier == "MEDIUM"  # base tier of FIX_DATA_QUALITY wins over the declared LOW


def test_pii_in_derived_table_blocked(gov):
    p = mk(operation="CREATE_DERIVED_TABLE", output_table="analytics.x",
           sql="SELECT customer_email, sum(amount) AS s FROM source GROUP BY ALL")
    assert "pii_derived_masking" in failed(static_check(p, gov.policy, gov.catalog))
    star = mk(operation="CREATE_DERIVED_TABLE", output_table="analytics.y", sql="SELECT * FROM source")
    assert "pii_derived_masking" in failed(static_check(star, gov.policy, gov.catalog))


def test_hashed_pii_in_derived_table_allowed(gov):
    p = mk(operation="CREATE_DERIVED_TABLE", output_table="analytics.x",
           sql="SELECT sha256(customer_email) AS customer_sha256, sum(amount) AS s FROM source GROUP BY ALL")
    assert static_check(p, gov.policy, gov.catalog).allowed


def test_in_place_pii_touch_escalates_and_eval_catches_it(gov):
    # a narrow change (few rows, under the blast-radius limit) that quietly rewrites PII values
    p = mk(sql="SELECT * REPLACE (CASE WHEN amount > 900 THEN upper(customer_email) ELSE customer_email END AS customer_email) FROM source",
           risk_tier="MEDIUM")
    assert static_check(p, gov.policy, gov.catalog).risk_tier == "HIGH"
    out = gov.run_plan(p, {}, approver="mrm", approver_role="elevated", simulated_decision="yes")
    assert out.status == "EVAL_FAILED" and "pii_values_unchanged" in out.detail


def test_pii_like_new_column_escalates(gov):
    p = mk(operation="ADD_COLUMN", sql="SELECT *, 'x' AS customer_phone FROM source",
           new_columns=[{"name": "customer_phone", "type": "string"}])
    assert static_check(p, gov.policy, gov.catalog).risk_tier == "HIGH"


def test_blast_radius_hard_limit_blocks_mass_delete(gov):
    out = approve(gov, plan_for(gov, "purge q1 old history"), approver_role="elevated")
    assert out.status == "BLOCKED_BY_POLICY" and "hard limit" in out.detail


def test_sandbox_blocks_file_access(gov):
    p = mk(sql="SELECT * FROM read_csv('/etc/passwd')", risk_tier="MEDIUM")
    out = gov.run_plan(p, {}, approver="r", simulated_decision="yes")
    assert out.status == "EVAL_FAILED" and "transform failed" in out.detail


def test_expected_effect_must_be_met(gov):
    p = mk(sql="SELECT * REPLACE (upper(trim(currency)) AS currency) FROM source", risk_tier="MEDIUM",
           expected_effect={"duplicate_txn_id": 0})
    out = gov.run_plan(p, {}, approver="r", simulated_decision="yes")
    assert out.status == "EVAL_FAILED" and "expected_effect:duplicate_txn_id" in out.detail


def test_dq_regression_blocked(gov):
    p = mk(sql="SELECT * REPLACE (CASE WHEN amount > 100 THEN NULL ELSE merchant_category END AS merchant_category) FROM source",
           risk_tier="MEDIUM")
    out = gov.run_plan(p, {}, approver="r", simulated_decision="yes")
    assert out.status in ("EVAL_FAILED", "BLOCKED_BY_POLICY")
    assert "dq_no_regression" in out.detail or "hard limit" in out.detail


def test_existing_column_cannot_be_re_added(gov):
    approve(gov, plan_for(gov, "add amount band"))
    pair = plan_for(gov, "add amount band")
    out = approve(gov, pair)
    assert out.status == "BLOCKED_BY_POLICY" and "already exist" in out.detail
