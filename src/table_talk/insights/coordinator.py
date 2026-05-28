from __future__ import annotations

import uuid
from datetime import datetime, timezone

from table_talk.insights.agents.analyst import Analyst
from table_talk.insights.agents.researcher import Researcher
from table_talk.insights.agents.skeptic import Skeptic
from table_talk.insights.clients.bigquery import BigQueryClient
from table_talk.insights.contracts import (
    AnalysisMethod,
    FindingProvenance,
    PublishedFinding,
    VerdictOutcome,
)
from table_talk.insights.statistician import compute_statistics


class Coordinator:
    """
    Runs the end-to-end Insights Factory loop. The only component that knows
    the overall flow exists — agents stay ignorant of orchestration.
    """

    def __init__(
        self,
        researcher: Researcher,
        analyst: Analyst,
        skeptic: Skeptic,
        bq_client: BigQueryClient,
        max_revisions: int = 1,
    ) -> None:
        self._researcher = researcher
        self._analyst = analyst
        self._skeptic = skeptic
        self._bq = bq_client
        self._max_revisions = max_revisions

    def investigate(self, question: str, motivation: str = "self-initiated") -> PublishedFinding:
        hypothesis = self._researcher.frame(question, motivation)

        verdict = None
        results = None

        for _ in range(self._max_revisions + 1):
            sql = self._analyst.write_sql(hypothesis)

            dry = self._bq.dry_run_query(sql)
            if not dry.valid:
                raise ValueError(f"Analyst produced invalid SQL: {dry.error}")

            query_result = self._bq.execute_query(sql)
            results = compute_statistics(
                query_result.rows,
                hypothesis,
                query_metadata={"bytes_processed": query_result.bytes_processed},
            )

            verdict = self._skeptic.critique(hypothesis, results)

            if verdict.outcome != VerdictOutcome.REVISE:
                break

            # Placeholder revision: re-run with the same hypothesis.
            # TODO: feed SkepticVerdict.revision_requests back to Researcher
            # for a genuine hypothesis revision rather than a bare retry.

        interpretation = self._researcher.interpret(hypothesis, results)

        return PublishedFinding(
            finding_id=str(uuid.uuid4()),
            hypothesis=hypothesis,
            results=results,
            interpretation=interpretation,
            caveats=verdict.caveats,
            provenance=FindingProvenance(
                created_at=datetime.now(timezone.utc),
                motivation=motivation,
                method=AnalysisMethod.SQL_ONLY,
            ),
        )
