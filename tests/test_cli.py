"""End-to-end through the real CLI entry point, including the interactive approval prompt."""
import json
import subprocess
import sys


def gia(home, *args, stdin=""):
    return subprocess.run([sys.executable, "-m", "governed_agent", "--home", str(home), *args],
                          input=stdin, capture_output=True, text=True, timeout=120)


def test_cli_prompt_approve_audit_rollback(tmp_path):
    home = tmp_path / "h"
    assert gia(home, "init", "--rows", "800").returncode == 0
    r = gia(home, "run", "--goal", "standardize currency codes", "--planner", "mock", "--approver", "alex",
            stdin="y\nCHG-42 diff reviewed\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Approve plan" in r.stdout and "[EXECUTED]" in r.stdout
    recs = [json.loads(l) for l in (home / "audit" / "audit.jsonl").read_text().splitlines()]
    appr = [x for x in recs if x["event_type"] == "APPROVAL_RECORDED"][0]["payload"]
    assert appr["approver"] == "alex" and appr["channel"] == "interactive" and appr["reason"] == "CHG-42 diff reviewed"
    plan_id = appr and [x for x in recs if x["event_type"] == "EXECUTED"][0]["plan_id"]
    assert gia(home, "audit", "--verify").returncode == 0
    r = gia(home, "rollback", plan_id, "--approver", "ops.lead")
    assert r.returncode == 0 and "restored_snapshot_id" in r.stdout


def test_cli_non_interactive_fails_closed(tmp_path):
    home = tmp_path / "h"
    gia(home, "init", "--rows", "800")
    r = gia(home, "run", "--goal", "standardize currency codes", "--planner", "mock", "--approver", "alex", stdin="")
    assert r.returncode == 3 and "[REJECTED]" in r.stdout
