from __future__ import annotations

import json
from typing import Any

from table_talk.insights.agents.base import Agent
from table_talk.insights.clients.llm import LLMClient
from table_talk.insights.contracts import Hypothesis, ResultsObject, SkepticVerdict

_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["APPROVED", "REVISE", "APPROVED_WITH_CAVEATS"],
        },
        "caveats": {"type": "array", "items": {"type": "string"}},
        "revision_requests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["target", "reason"],
            },
        },
    },
    "required": ["outcome", "caveats", "revision_requests"],
}


class Skeptic(Agent):
    def __init__(self, llm: LLMClient) -> None:
        super().__init__(llm, "skeptic.md")

    def critique(self, hypothesis: Hypothesis, results: ResultsObject) -> SkepticVerdict:
        """
        Critique a hypothesis/results pair. Motivation is ALWAYS redacted before
        sending to the LLM — analytical judgment must not be biased by investigative
        provenance. This is enforced in code, not just in the prompt.
        """
        h_dict = hypothesis.to_dict()
        h_dict["motivation"] = "[REDACTED]"  # integrity firewall

        user_message = json.dumps(
            {"hypothesis": h_dict, "results": results.to_dict()},
            indent=2,
        )
        response_text = self._llm.complete(
            self._system_prompt, user_message, response_schema=_VERDICT_SCHEMA
        )
        return SkepticVerdict.from_dict(json.loads(response_text))
