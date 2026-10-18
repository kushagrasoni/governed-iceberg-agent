"""OpenAI-compatible chat-completions planner (works with any /v1/chat/completions endpoint,
including self-hosted gateways). Configure with env vars:
  GIA_LLM_BASE_URL (default https://api.openai.com/v1), GIA_LLM_API_KEY (or OPENAI_API_KEY),
  GIA_LLM_MODEL (default gpt-4o-mini).
The response is treated as untrusted input: it is parsed, validated, policy-checked and evaluated
exactly like any other plan."""
from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import urlparse

from .base import PlannerOutput, build_prompt, sha256


class LLMPlanner:
    name = "llm-planner"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "LLMPlanner":
        key = os.environ.get("GIA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("LLM planner needs GIA_LLM_API_KEY or OPENAI_API_KEY; use --planner mock to run offline")
        return cls(os.environ.get("GIA_LLM_BASE_URL", "https://api.openai.com/v1"), key,
                   os.environ.get("GIA_LLM_MODEL", "gpt-4o-mini"))

    def propose(self, goal: str, context: dict) -> PlannerOutput:
        system, user = build_prompt(goal, context)
        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        content = payload["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM did not return JSON: {e}") from e
        plans = parsed.get("plans", [parsed] if "operation" in parsed else [])
        prov = {
            "planner": self.name,
            "model": payload.get("model", self.model),
            "model_requested": self.model,
            "model_version": payload.get("system_fingerprint") or payload.get("model", self.model),
            "response_id": payload.get("id"),
            "endpoint": urlparse(self.base_url).netloc,
            "temperature": 0,
            "usage": payload.get("usage"),
            "system_prompt_sha256": sha256(system),
            "user_prompt_sha256": sha256(user),
            "response_sha256": sha256(content),
        }
        return PlannerOutput(plans, prov, system, user, content)
