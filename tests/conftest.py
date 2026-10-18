import warnings

import pytest

from governed_agent.config import get_settings
from governed_agent.pipeline import Governor

warnings.filterwarnings("ignore", message="Delete operation did not match any records")


@pytest.fixture
def gov(tmp_path):
    g = Governor(get_settings(tmp_path / "home"), print_fn=lambda *_: None)
    g.init(rows=1500, seed=11)
    return g


def plan_for(g, goal):
    pairs = g.propose(goal, "mock")
    assert pairs, f"mock planner returned nothing for {goal!r}"
    return pairs[0]


def approve(g, pair, **kw):
    kw.setdefault("approver", "reviewer.test")
    kw.setdefault("simulated_decision", "yes")
    return g.run_plan(pair[0], pair[1], **kw)
