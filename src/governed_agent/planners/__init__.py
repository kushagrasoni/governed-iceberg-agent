"""Planners turn a goal + table context into structured plans. They never execute anything."""
from __future__ import annotations

import os

from .base import PlannerOutput, build_prompt  # noqa: F401
from .llm import LLMPlanner
from .mock import MockPlanner


def get_planner(kind: str = "auto"):
    """'mock' (offline, deterministic), 'llm' (OpenAI-compatible), or 'auto' (llm if a key is set)."""
    if kind == "mock":
        return MockPlanner()
    if kind == "llm":
        return LLMPlanner.from_env()
    if kind == "auto":
        return LLMPlanner.from_env() if (os.environ.get("GIA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")) else MockPlanner()
    raise ValueError(f"unknown planner {kind!r}")
