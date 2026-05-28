from __future__ import annotations

import json
from typing import Any

from table_talk.insights.agents.base import Agent
from table_talk.insights.clients.llm import LLMClient
from table_talk.insights.contracts import Hypothesis, ResultsObject

# JSON schema for structured output — constrains Gemini to the Hypothesis shape
# (motivation is NOT included; it is injected by this class after parsing).
_HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "primary_metric": {"type": "string"},
        "stratification": {"type": "array", "items": {"type": "string"}},
        "minimum_sample_per_cell": {"type": "integer"},
        "expected_direction": {"type": "string"},
        "comparison_groups": {"type": "array", "items": {"type": "string"}},
        "canonicalization": {
            "type": "object",
            "properties": {
                "version": {"type": "string"},
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "concept": {"type": "string"},
                            "chosen_definition": {"type": "string"},
                            "version": {"type": "string"},
                            "rejected_alternatives": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "definition": {"type": "string"},
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["definition", "reason"],
                                },
                            },
                        },
                        "required": [
                            "concept",
                            "chosen_definition",
                            "version",
                            "rejected_alternatives",
                        ],
                    },
                },
            },
            "required": ["version", "choices"],
        },
    },
    "required": [
        "claim",
        "primary_metric",
        "stratification",
        "minimum_sample_per_cell",
        "canonicalization",
    ],
}


class Researcher(Agent):
    def __init__(self, llm: LLMClient) -> None:
        super().__init__(llm, "researcher.md")

    def frame(self, question: str, motivation: str) -> Hypothesis:
        """Turn a question into a structured Hypothesis. Motivation is injected after LLM response."""
        user_message = f"Question: {question}"
        response_text = self._llm.complete(
            self._system_prompt, user_message, response_schema=_HYPOTHESIS_SCHEMA
        )
        data = json.loads(response_text)
        data["motivation"] = motivation
        return Hypothesis.from_dict(data)

    def interpret(self, hypothesis: Hypothesis, results: ResultsObject) -> str:
        """Produce descriptive prose summarizing what the results show."""
        user_message = json.dumps(
            {"hypothesis": hypothesis.to_dict(), "results": results.to_dict()},
            indent=2,
        )
        return self._llm.complete(self._system_prompt, user_message)
