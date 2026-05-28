"""
Integration test for the full Insights Factory pipeline with real Gemini calls.

Costs a few cents per run. Requires GOOGLE_API_KEY or GEMINI_API_KEY in the
environment. Skipped by default (marked @pytest.mark.integration).

Run with: uv run pytest -m integration tests/insights/test_orchestration_integration.py -v
"""
import os

import pytest


@pytest.mark.integration
def test_real_gemini_end_to_end() -> None:
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("GOOGLE_API_KEY or GEMINI_API_KEY not set in environment")

    from table_talk.insights.agents.analyst import Analyst
    from table_talk.insights.agents.researcher import Researcher
    from table_talk.insights.agents.skeptic import Skeptic
    from table_talk.insights.clients.bigquery import FakeBigQueryClient
    from table_talk.insights.clients.llm import GeminiLLMClient
    from table_talk.insights.contracts import AnalysisMethod
    from table_talk.insights.coordinator import Coordinator

    llm = GeminiLLMClient(api_key=api_key)
    bq = FakeBigQueryClient()

    coordinator = Coordinator(
        researcher=Researcher(llm),
        analyst=Analyst(llm),
        skeptic=Skeptic(llm),
        bq_client=bq,
    )

    finding = coordinator.investigate(
        question="How does shove frequency vary by position among short-stack players?",
        motivation="Integration test — validating the full Insights Factory pipeline.",
    )

    # Loose assertions — real LLM output varies
    assert finding.finding_id
    assert finding.hypothesis.motivation == (
        "Integration test — validating the full Insights Factory pipeline."
    )
    assert len(finding.results.cells) > 0
    assert finding.interpretation
    assert finding.provenance.method == AnalysisMethod.SQL_ONLY
    assert finding.provenance.motivation == (
        "Integration test — validating the full Insights Factory pipeline."
    )
