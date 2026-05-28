import json

import pytest

from table_talk.insights.agents.base import _PROMPTS_DIR
from table_talk.insights.agents.researcher import Researcher
from table_talk.insights.clients.llm import StubLLMClient
from table_talk.insights.contracts import (
    AnalysisMethod,
    CellResult,
    ResultsObject,
)


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


_SYS = _load_prompt("researcher.md")

_HYPOTHESIS_RESPONSE = json.dumps({
    "claim": "Short-stack BTN players shove more than BB players.",
    "primary_metric": "shove_rate",
    "stratification": ["position"],
    "minimum_sample_per_cell": 10,
    "canonicalization": {
        "choices": [{
            "concept": "short stack",
            "chosen_definition": "starting_stack_bb <= 15",
            "version": "v1",
            "rejected_alternatives": [{"definition": "starting_stack_bb < 12", "reason": "too restrictive"}],
        }],
        "version": "v1",
    },
    "comparison_groups": ["BTN", "BB"],
    "expected_direction": "BTN > BB",
})


def _make_results() -> ResultsObject:
    return ResultsObject(
        cells=[CellResult(
            dimensions={"position": "BTN"},
            point_estimate=0.72,
            ci_low=0.72,
            ci_high=0.72,
            sample_size=50,
            below_min_sample=False,
        )],
        total_sample_size=50,
        method=AnalysisMethod.SQL_ONLY,
        query_metadata={},
    )


# --- frame ---

def test_frame_returns_hypothesis() -> None:
    stub = StubLLMClient({(_SYS, "Question: test question"): _HYPOTHESIS_RESPONSE})
    researcher = Researcher(stub)
    h = researcher.frame("test question", "testing motivation")
    assert h.claim == "Short-stack BTN players shove more than BB players."
    assert h.primary_metric == "shove_rate"
    assert h.stratification == ["position"]


def test_frame_injects_motivation() -> None:
    stub = StubLLMClient({(_SYS, "Question: my question"): _HYPOTHESIS_RESPONSE})
    researcher = Researcher(stub)
    h = researcher.frame("my question", "the real reason I care")
    assert h.motivation == "the real reason I care"


def test_frame_injects_motivation_not_from_llm() -> None:
    # motivation does NOT come from the LLM response — it's a separate parameter
    stub = StubLLMClient({(_SYS, "Question: q"): _HYPOTHESIS_RESPONSE})
    researcher = Researcher(stub)
    h = researcher.frame("q", "injected externally")
    assert h.motivation == "injected externally"


def test_frame_raises_on_malformed_json() -> None:
    stub = StubLLMClient({(_SYS, "Question: q"): "not valid json {"})
    researcher = Researcher(stub)
    with pytest.raises(Exception):  # json.JSONDecodeError
        researcher.frame("q", "any motivation")


def test_frame_raises_when_no_stub_registered() -> None:
    stub = StubLLMClient({})
    researcher = Researcher(stub)
    with pytest.raises(KeyError):
        researcher.frame("unregistered question", "motivation")


# --- interpret ---

def test_interpret_returns_prose() -> None:
    results = _make_results()
    hypothesis_dict = json.loads(_HYPOTHESIS_RESPONSE)
    hypothesis_dict["motivation"] = "test"
    from table_talk.insights.contracts import Hypothesis
    hypothesis = Hypothesis.from_dict(hypothesis_dict)

    user_message = json.dumps(
        {"hypothesis": hypothesis.to_dict(), "results": results.to_dict()},
        indent=2,
    )
    stub = StubLLMClient({(_SYS, user_message): "BTN players shoved more than BB players."})
    researcher = Researcher(stub)
    prose = researcher.interpret(hypothesis, results)
    assert prose == "BTN players shoved more than BB players."
