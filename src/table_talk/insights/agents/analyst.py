from __future__ import annotations

import json
from typing import Any

from table_talk.insights.agents.base import Agent
from table_talk.insights.clients.llm import LLMClient
from table_talk.insights.contracts import Hypothesis

_SQL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
}


class Analyst(Agent):
    def __init__(self, llm: LLMClient) -> None:
        super().__init__(llm, "analyst.md")

    def write_sql(self, hypothesis: Hypothesis) -> str:
        """Produce a SQL query that evaluates the hypothesis. Returns the raw SQL string."""
        user_message = json.dumps(hypothesis.to_dict(), indent=2)
        response_text = self._llm.complete(
            self._system_prompt, user_message, response_schema=_SQL_SCHEMA
        )
        return json.loads(response_text)["sql"]
