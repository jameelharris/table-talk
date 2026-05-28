import json
from datetime import datetime, timezone

from table_talk.insights.contracts import (
    AnalysisMethod,
    CanonicalizationChoice,
    CanonicalizationManifest,
    CellResult,
    FindingProvenance,
    Hypothesis,
    PublishedFinding,
    ResultsObject,
    RevisionRequest,
    SkepticVerdict,
    StackBucket,
    VerdictOutcome,
)


# --- fixtures ---

def _canonicalization() -> CanonicalizationManifest:
    return CanonicalizationManifest(
        choices=[
            CanonicalizationChoice(
                concept="short stack",
                chosen_definition="stack_bb < 20",
                version="v1",
                rejected_alternatives=[
                    {"definition": "stack_bb < 15", "reason": "too restrictive"},
                ],
            )
        ],
        version="v1",
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        claim="Short stacks shove more frequently from the BTN than from the BB.",
        primary_metric="shove_frequency",
        stratification=["position", "stack_bucket"],
        minimum_sample_per_cell=30,
        canonicalization=_canonicalization(),
        motivation="Viewers ask about BTN shove ranges constantly; this validates the intuition.",
        comparison_groups=["BTN", "BB"],
        expected_direction="BTN > BB",
    )


def _results() -> ResultsObject:
    return ResultsObject(
        cells=[
            CellResult(
                dimensions={"position": "BTN", "stack_bucket": "short"},
                point_estimate=0.72,
                ci_low=0.65,
                ci_high=0.79,
                sample_size=150,
                below_min_sample=False,
            ),
            CellResult(
                dimensions={"position": "BB", "stack_bucket": "short"},
                point_estimate=0.45,
                ci_low=0.38,
                ci_high=0.52,
                sample_size=12,
                below_min_sample=True,
            ),
        ],
        total_sample_size=162,
        method=AnalysisMethod.SQL_ONLY,
        query_metadata={"bytes_processed": 5_000_000, "notes": "partition filtered by tournament_id"},
        cross_group_comparisons=[{"group_a": "BTN", "group_b": "BB", "difference": 0.27}],
    )


def _published_finding() -> PublishedFinding:
    return PublishedFinding(
        finding_id="finding-001",
        hypothesis=_hypothesis(),
        results=_results(),
        interpretation=(
            "BTN players with short stacks shove at a higher rate than BB players "
            "in the same stack range."
        ),
        caveats=["BB cell is below minimum sample size; treat as exploratory."],
        provenance=FindingProvenance(
            created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
            motivation="Viewers ask about BTN shove ranges constantly; this validates the intuition.",
            method=AnalysisMethod.SQL_ONLY,
        ),
    )


# --- enum membership ---

def test_stack_bucket_members():
    assert {b.value for b in StackBucket} == {"short", "medium", "deep", "very_deep"}


def test_analysis_method_members():
    assert set(AnalysisMethod) == {
        AnalysisMethod.SQL_ONLY,
        AnalysisMethod.PYTHON_COMPUTE,
        AnalysisMethod.SQL_PLUS_PYTHON,
    }


def test_verdict_outcome_members():
    assert set(VerdictOutcome) == {
        VerdictOutcome.APPROVED,
        VerdictOutcome.REVISE,
        VerdictOutcome.APPROVED_WITH_CAVEATS,
    }


# --- construction ---

def test_canonicalization_choice_construction():
    c = _canonicalization().choices[0]
    assert c.concept == "short stack"
    assert c.rejected_alternatives[0]["reason"] == "too restrictive"


def test_hypothesis_construction():
    h = _hypothesis()
    assert h.minimum_sample_per_cell == 30
    assert h.comparison_groups == ["BTN", "BB"]
    assert h.expected_direction == "BTN > BB"


def test_results_object_below_min_sample_flag():
    r = _results()
    assert r.cells[0].below_min_sample is False
    assert r.cells[1].below_min_sample is True
    assert r.error is None


def test_published_finding_construction():
    f = _published_finding()
    assert f.finding_id == "finding-001"
    assert f.results.method == AnalysisMethod.SQL_ONLY


# --- serialization round-trips ---

def test_canonicalization_choice_round_trip():
    obj = _canonicalization().choices[0]
    assert CanonicalizationChoice.from_dict(obj.to_dict()) == obj


def test_canonicalization_manifest_round_trip():
    obj = _canonicalization()
    assert CanonicalizationManifest.from_dict(obj.to_dict()) == obj


def test_hypothesis_round_trip():
    obj = _hypothesis()
    assert Hypothesis.from_dict(obj.to_dict()) == obj


def test_hypothesis_motivation_round_trip():
    obj = _hypothesis()
    d = obj.to_dict()
    assert d["motivation"] == "Viewers ask about BTN shove ranges constantly; this validates the intuition."
    restored = Hypothesis.from_dict(d)
    assert restored.motivation == obj.motivation


def test_hypothesis_optional_fields_none_round_trip():
    obj = Hypothesis(
        claim="Test claim.",
        primary_metric="fold_frequency",
        stratification=["position"],
        minimum_sample_per_cell=20,
        canonicalization=_canonicalization(),
        motivation="Research question.",
    )
    restored = Hypothesis.from_dict(obj.to_dict())
    assert restored == obj
    assert restored.comparison_groups is None
    assert restored.expected_direction is None


def test_cell_result_round_trip():
    obj = _results().cells[0]
    assert CellResult.from_dict(obj.to_dict()) == obj


def test_results_object_round_trip():
    obj = _results()
    assert ResultsObject.from_dict(obj.to_dict()) == obj


def test_results_object_enum_serializes_to_string():
    d = _results().to_dict()
    assert d["method"] == "SQL_ONLY"


def test_results_object_with_error_round_trip():
    obj = ResultsObject(
        cells=[],
        total_sample_size=0,
        method=AnalysisMethod.SQL_ONLY,
        query_metadata={"notes": "query timeout"},
        error="Query exceeded byte budget",
    )
    assert ResultsObject.from_dict(obj.to_dict()) == obj


def test_skeptic_verdict_round_trip():
    obj = SkepticVerdict(
        outcome=VerdictOutcome.APPROVED_WITH_CAVEATS,
        caveats=["BB cell below minimum sample."],
        revision_requests=[RevisionRequest(target="sample_size", reason="BB cell too small")],
    )
    assert SkepticVerdict.from_dict(obj.to_dict()) == obj


def test_finding_provenance_round_trip():
    obj = FindingProvenance(
        created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
        motivation="test motivation",
        method=AnalysisMethod.SQL_ONLY,
    )
    assert FindingProvenance.from_dict(obj.to_dict()) == obj


def test_published_finding_round_trip():
    obj = _published_finding()
    assert PublishedFinding.from_dict(obj.to_dict()) == obj


def test_published_finding_json_round_trip():
    obj = _published_finding()
    restored = PublishedFinding.from_dict(json.loads(json.dumps(obj.to_dict())))
    assert restored == obj


def test_published_finding_datetime_serializes_to_string():
    d = _published_finding().to_dict()
    assert isinstance(d["provenance"]["created_at"], str)
    assert d["provenance"]["created_at"] == "2026-05-28T12:00:00+00:00"
