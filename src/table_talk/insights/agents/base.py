from __future__ import annotations

from pathlib import Path

from table_talk.insights.clients.llm import LLMClient

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class Agent:
    def __init__(self, llm: LLMClient, prompt_file: str) -> None:
        self._llm = llm
        self._system_prompt = (_PROMPTS_DIR / prompt_file).read_text()
