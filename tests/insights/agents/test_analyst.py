import json

import pytest

from table_talk.insights.agents.analyst import Analyst
from table_talk.insights.agents.base import _PROMPTS_DIR
from table_talk.insights.clients.llm import StubLLMClient
from table_talk.insights.contracts import (
    CanonicalizationChoice,
    CanonicalizationManifest,
    Hypothesis,
)


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


_SYS = _load_prompt("analyst.md")

_HYPOTHESIS = Hypothesis(
    claim="BTN shovers more than BB at short stacks.",
    primary_metric="shove_rate",
    stratification=["position"],
    minimum_sample_per_cell=5,
    canonicalization=CanonicalizationManifest(
        choices=[CanonicalizationChoice(
            concept="short stack",
            chosen_definition="starting_stack_bb <= 15",
            version="v1",
            rejected_alternatives=[{"definition": "< 12", "reason": "too tight"}],
        )],
        version="v1",
    ),
    motivation="test motivation",
    comparison_groups=["BTN", "BB"],
)

_SQL = "SELECT position, CAST(SUM(CASE WHEN fva_action = 'all_in' THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) AS metric, COUNT(*) AS sample_size FROM hands WHERE position IN ('BTN', 'BB') GROUP BY position"


def test_write_sql_returns_sql_string() -> None:
    user_message = json.dumps(_HYPOTHESIS.to_dict(), indent=2)
    stub = StubLLMClient({(_SYS, user_message): json.dumps({"sql": _SQL})})
    analyst = Analyst(stub)
    result = analyst.write_sql(_HYPOTHESIS)
    assert result == _SQL


def test_write_sql_raises_on_malformed_json() -> None:
    user_message = json.dumps(_HYPOTHESIS.to_dict(), indent=2)
    stub = StubLLMClient({(_SYS, user_message): "bad json {"})
    analyst = Analyst(stub)
    with pytest.raises(Exception):
        analyst.write_sql(_HYPOTHESIS)


def test_write_sql_raises_on_missing_sql_key() -> None:
    user_message = json.dumps(_HYPOTHESIS.to_dict(), indent=2)
    stub = StubLLMClient({(_SYS, user_message): json.dumps({"not_sql": "SELECT 1"})})
    analyst = Analyst(stub)
    with pytest.raises(KeyError):
        analyst.write_sql(_HYPOTHESIS)


def test_write_sql_raises_when_no_stub_registered() -> None:
    stub = StubLLMClient({})
    analyst = Analyst(stub)
    with pytest.raises(KeyError):
        analyst.write_sql(_HYPOTHESIS)
