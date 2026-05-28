import json

import pytest

from table_talk.insights.agents.base import _PROMPTS_DIR
from table_talk.insights.agents.skeptic import Skeptic
from table_talk.insights.clients.llm import StubLLMClient
from table_talk.insights.contracts import (
    AnalysisMethod,
    CanonicalizationChoice,
    CanonicalizationManifest,
    CellResult,
    Hypothesis,
    ResultsObject,
    VerdictOutcome,
)


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


_SYS = _load_prompt("skeptic.md")


def _make_hypothesis(motivation: str = "test motivation") -> Hypothesis:
    return Hypothesis(
        claim="BTN short stacks shove more than BB.",
        primary_metric="shove_rate",
        stratification=["position"],
        minimum_sample_per_cell=10,
        canonicalization=CanonicalizationManifest(
            choices=[CanonicalizationChoice(
                concept="short stack",
                chosen_definition="starting_stack_bb <= 15",
                version="v1",
                rejected_alternatives=[],
            )],
            version="v1",
        ),
        motivation=motivation,
    )


def _make_results(sample_size: int = 50) -> ResultsObject:
    return ResultsObject(
        cells=[CellResult(
            dimensions={"position": "BTN"},
            point_estimate=0.7,
            ci_low=0.7,
            ci_high=0.7,
            sample_size=sample_size,
            below_min_sample=sample_size < 10,
        )],
        total_sample_size=sample_size,
        method=AnalysisMethod.SQL_ONLY,
        query_metadata={},
    )


def _redacted_user_message(hypothesis: Hypothesis, results: ResultsObject) -> str:
    h_dict = hypothesis.to_dict()
    h_dict["motivation"] = "[REDACTED]"
    return json.dumps({"hypothesis": h_dict, "results": results.to_dict()}, indent=2)


# --- happy path ---

def test_critique_returns_approved_verdict() -> None:
    hypothesis = _make_hypothesis()
    results = _make_results()
    user_message = _redacted_user_message(hypothesis, results)
    verdict_json = json.dumps({
        "outcome": "APPROVED",
        "caveats": [],
        "revision_requests": [],
    })
    stub = StubLLMClient({(_SYS, user_message): verdict_json})
    skeptic = Skeptic(stub)
    verdict = skeptic.critique(hypothesis, results)
    assert verdict.outcome == VerdictOutcome.APPROVED
    assert verdict.caveats == []


def test_critique_returns_revise_verdict() -> None:
    hypothesis = _make_hypothesis()
    results = _make_results(sample_size=2)  # below minimum
    user_message = _redacted_user_message(hypothesis, results)
    verdict_json = json.dumps({
        "outcome": "REVISE",
        "caveats": ["Primary cell has insufficient sample size."],
        "revision_requests": [{"target": "minimum_sample_per_cell", "reason": "Only 2 observations."}],
    })
    stub = StubLLMClient({(_SYS, user_message): verdict_json})
    skeptic = Skeptic(stub)
    verdict = skeptic.critique(hypothesis, results)
    assert verdict.outcome == VerdictOutcome.REVISE
    assert len(verdict.revision_requests) == 1


def test_critique_returns_approved_with_caveats() -> None:
    hypothesis = _make_hypothesis()
    results = _make_results()
    user_message = _redacted_user_message(hypothesis, results)
    verdict_json = json.dumps({
        "outcome": "APPROVED_WITH_CAVEATS",
        "caveats": ["Table size not controlled."],
        "revision_requests": [],
    })
    stub = StubLLMClient({(_SYS, user_message): verdict_json})
    skeptic = Skeptic(stub)
    verdict = skeptic.critique(hypothesis, results)
    assert verdict.outcome == VerdictOutcome.APPROVED_WITH_CAVEATS
    assert "Table size not controlled." in verdict.caveats


# --- integrity firewall test ---

def test_skeptic_firewall_redacts_motivation() -> None:
    """
    Critical firewall test. The hypothesis carries motivation="SENTINEL_MOTIVATION_DO_NOT_LEAK".
    The stub is registered ONLY with the redacted user message (motivation="[REDACTED]").

    If Skeptic.critique passes the unredacted motivation to the LLM, the user message
    won't match the registered key → StubLLMClient raises KeyError → test fails.
    Reaching the assertion proves the firewall works in code.
    """
    hypothesis = _make_hypothesis(motivation="SENTINEL_MOTIVATION_DO_NOT_LEAK")
    results = _make_results()

    # Build the REDACTED user message — this is what the Skeptic SHOULD send
    redacted_msg = _redacted_user_message(hypothesis, results)
    verdict_json = json.dumps({
        "outcome": "APPROVED",
        "caveats": [],
        "revision_requests": [],
    })
    stub = StubLLMClient({(_SYS, redacted_msg): verdict_json})
    skeptic = Skeptic(stub)

    # If firewall fails: stub raises KeyError (unredacted key not registered)
    # If firewall works: verdict is returned normally
    verdict = skeptic.critique(hypothesis, results)
    assert verdict.outcome == VerdictOutcome.APPROVED


def test_critique_raises_on_malformed_json() -> None:
    hypothesis = _make_hypothesis()
    results = _make_results()
    user_message = _redacted_user_message(hypothesis, results)
    stub = StubLLMClient({(_SYS, user_message): "not json {"})
    skeptic = Skeptic(stub)
    with pytest.raises(Exception):
        skeptic.critique(hypothesis, results)
