"""`gia demo` story mode (default), --pause, and the plain --fast mode used by CI."""
import subprocess
import sys

CHAPTER_TITLES = ["Data and seeded issues", "The agent proposes", "Guardrails block risky plans", "Branch dry-run and evals",
                  "Human approval gate", "Document AI confidence routing", "Evidence trail", "Rollback"]


def gia(home, *args, stdin=""):
    return subprocess.run([sys.executable, "-m", "governed_agent", "--home", str(home), *args],
                          input=stdin, capture_output=True, text=True, timeout=180)


def test_story_mode_runs_all_chapters_and_scorecard(tmp_path):
    home = tmp_path / "h"
    r = gia(home, "demo")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    for i, title in enumerate(CHAPTER_TITLES, 1):
        assert f"{i}/8  {title}" in out, title
    assert out.count("Why it matters for a bank:") == 8
    assert "Scorecard" in out and "CHAIN INTACT" in out and "REVIEWABLE" in out
    assert "BLOCKED" in out and "REFUSED" in out and "RESTORED" in out
    assert "Enter for chapter" not in out  # no pausing unless asked
    assert gia(home, "audit", "--verify").returncode == 0


def test_pause_waits_for_enter_and_survives_eof(tmp_path):
    r = gia(tmp_path / "a", "demo", "--pause", stdin="\n" * 40)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Enter for chapter 2: The agent proposes" in r.stdout and "Enter for the scorecard" in r.stdout
    # no stdin at all (CI, pipes): the first prompt hits EOF and the demo carries on without hanging
    r = gia(tmp_path / "b", "demo", "--pause", stdin="")
    assert r.returncode == 0 and "Scorecard" in r.stdout


def test_fast_mode_is_plain_and_rejects_pause(tmp_path):
    r = gia(tmp_path / "h", "demo", "--fast")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "=== 1. Init local Iceberg lakehouse" in r.stdout and "[BLOCKED ]" in r.stdout
    assert "Scorecard" not in r.stdout
    r = gia(tmp_path / "h", "demo", "--fast", "--pause")
    assert r.returncode != 0 and "--pause only applies to story mode" in r.stderr
