"""
Keystone orchestration test. Uses StubLLMClient + FakeBigQueryClient.

The stub keys are computed by pre-running the same deterministic path the
Coordinator will take — same SQL against the same FakeBigQueryClient instance,
same compute_statistics call — so the ResultsObject serialization in the stub
key is byte-for-byte identical to what the Coordinator produces at runtime.
"""
from __future__ import annotations

import json

from table_talk.insights.agents.analyst import Analyst
from table_talk.insights.agents.base import _PROMPTS_DIR
from table_talk.insights.agents.researcher import Researcher
from table_talk.insights.agents.skeptic import Skeptic
from table_talk.insights.clients.bigquery import FakeBigQueryClient
from table_talk.insights.clients.llm import StubLLMClient
from table_talk.insights.contracts import (
    AnalysisMethod,
    Hypothesis,
)
from table_talk.insights.coordinator import Coordinator
from table_talk.insights.statistician import compute_statistics


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


# ── canned values ──────────────────────────────────────────────────────────────

_QUESTION = "How often do short-stack players shove all-in from the BTN versus the BB?"
_MOTIVATION = "Testing the orchestration end-to-end."

# What the LLM returns for Researcher.frame (motivation NOT included — injected later)
_RESEARCHER_FRAME_RESPONSE = json.dumps({
    "claim": "Players with short stacks shove all-in more frequently from the BTN than the BB.",
    "primary_metric": "shove_rate",
    "stratification": ["position"],
    "minimum_sample_per_cell": 3,
    "canonicalization": {
        "choices": [{
            "concept": "short stack",
            "chosen_definition": "starting_stack_bb <= 15",
            "version": "v1",
            "rejected_alternatives": [
                {"definition": "starting_stack_bb < 12", "reason": "too restrictive"},
            ],
        }],
        "version": "v1",
    },
    "comparison_groups": ["BTN", "BB"],
    "expected_direction": "BTN > BB",
})

# SQL the Analyst returns — must produce rows from the fixture
_SQL = (
    "SELECT position,"
    " CAST(SUM(CASE WHEN fva_action = 'all_in' THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) AS metric,"
    " COUNT(*) AS sample_size"
    " FROM hands"
    " WHERE position IN ('BTN', 'BB')"
    " GROUP BY position"
    " ORDER BY position"
)

_VERDICT_JSON = json.dumps({
    "outcome": "APPROVED",
    "caveats": ["Sample sizes are adequate for both groups."],
    "revision_requests": [],
})

_INTERPRETATION = (
    "BTN players in this sample show a shove rate of approximately 28.6%, "
    "compared to 14.3% for BB players, each group with 7 observations."
)


# ── test ───────────────────────────────────────────────────────────────────────

def test_full_orchestration() -> None:
    researcher_sys = _load_prompt("researcher.md")
    analyst_sys = _load_prompt("analyst.md")
    skeptic_sys = _load_prompt("skeptic.md")

    # Reconstruct the hypothesis exactly as the Coordinator will receive it
    hypothesis = Hypothesis.from_dict(
        {**json.loads(_RESEARCHER_FRAME_RESPONSE), "motivation": _MOTIVATION}
    )

    # Pre-run the SQL against the same FakeBigQueryClient to get deterministic results
    bq = FakeBigQueryClient()
    query_result = bq.execute_query(_SQL)
    results = compute_statistics(
        query_result.rows,
        hypothesis,
        query_metadata={"bytes_processed": query_result.bytes_processed},
    )

    # Build exact user messages for each stub entry
    researcher_frame_msg = f"Question: {_QUESTION}"
    analyst_msg = json.dumps(hypothesis.to_dict(), indent=2)

    h_dict_redacted = hypothesis.to_dict()
    h_dict_redacted["motivation"] = "[REDACTED]"
    skeptic_msg = json.dumps(
        {"hypothesis": h_dict_redacted, "results": results.to_dict()}, indent=2
    )

    researcher_interpret_msg = json.dumps(
        {"hypothesis": hypothesis.to_dict(), "results": results.to_dict()}, indent=2
    )

    stub = StubLLMClient({
        (researcher_sys, researcher_frame_msg): _RESEARCHER_FRAME_RESPONSE,
        (analyst_sys, analyst_msg): json.dumps({"sql": _SQL}),
        (skeptic_sys, skeptic_msg): _VERDICT_JSON,
        (researcher_sys, researcher_interpret_msg): _INTERPRETATION,
    })

    coordinator = Coordinator(
        researcher=Researcher(stub),
        analyst=Analyst(stub),
        skeptic=Skeptic(stub),
        bq_client=bq,
    )

    finding = coordinator.investigate(_QUESTION, _MOTIVATION)

    # Hypothesis preserved
    assert finding.hypothesis.claim == (
        "Players with short stacks shove all-in more frequently from the BTN than the BB."
    )
    assert finding.hypothesis.motivation == _MOTIVATION

    # Results from real FakeBigQueryClient — 2 position groups (BB, BTN)
    assert len(finding.results.cells) == 2
    positions = {c.dimensions["position"] for c in finding.results.cells}
    assert positions == {"BTN", "BB"}
    assert finding.results.total_sample_size == 14
    assert finding.results.method == AnalysisMethod.SQL_ONLY

    # No cells below threshold (7 >= 3)
    assert all(not c.below_min_sample for c in finding.results.cells)

    # Skeptic caveats propagated
    assert finding.caveats == ["Sample sizes are adequate for both groups."]

    # Interpretation from Researcher
    assert finding.interpretation == _INTERPRETATION

    # Provenance
    assert finding.provenance.motivation == _MOTIVATION
    assert finding.provenance.method == AnalysisMethod.SQL_ONLY
    assert finding.finding_id  # non-empty UUID string


def test_orchestration_propagates_invalid_sql_error() -> None:
    """Coordinator raises ValueError when Analyst returns SQL that fails dry-run."""
    researcher_sys = _load_prompt("researcher.md")
    analyst_sys = _load_prompt("analyst.md")

    hypothesis = Hypothesis.from_dict(
        {**json.loads(_RESEARCHER_FRAME_RESPONSE), "motivation": _MOTIVATION}
    )

    bad_sql = "THIS IS NOT VALID SQL"
    bq = FakeBigQueryClient()

    stub = StubLLMClient({
        (researcher_sys, f"Question: {_QUESTION}"): _RESEARCHER_FRAME_RESPONSE,
        (analyst_sys, json.dumps(hypothesis.to_dict(), indent=2)): json.dumps({"sql": bad_sql}),
    })

    import pytest
    coordinator = Coordinator(
        researcher=Researcher(stub),
        analyst=Analyst(stub),
        skeptic=Skeptic(stub),
        bq_client=bq,
    )
    with pytest.raises(ValueError, match="invalid SQL"):
        coordinator.investigate(_QUESTION, _MOTIVATION)
