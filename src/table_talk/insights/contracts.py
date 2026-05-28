from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any


def _to_serializable(val: Any) -> Any:
    """Recursively convert enums and datetimes to JSON-safe primitives."""
    if isinstance(val, Enum):
        return val.value
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, dict):
        return {k: _to_serializable(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_to_serializable(v) for v in val]
    return val


class StackBucket(Enum):
    SHORT = "short"
    MEDIUM = "medium"
    DEEP = "deep"
    VERY_DEEP = "very_deep"


@dataclass(frozen=True)
class CanonicalizationChoice:
    """One decision mapping a fuzzy poker concept to a concrete data predicate."""

    concept: str
    chosen_definition: str
    version: str
    rejected_alternatives: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CanonicalizationChoice:
        return cls(
            concept=d["concept"],
            chosen_definition=d["chosen_definition"],
            version=d["version"],
            rejected_alternatives=d["rejected_alternatives"],
        )


@dataclass(frozen=True)
class CanonicalizationManifest:
    """Audit trail of how domain language became data predicates."""

    choices: list[CanonicalizationChoice]
    version: str

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CanonicalizationManifest:
        return cls(
            choices=[CanonicalizationChoice.from_dict(c) for c in d["choices"]],
            version=d["version"],
        )


@dataclass(frozen=True)
class Hypothesis:
    """
    Central contract produced by the Researcher.

    `motivation` records WHY this is being investigated. It is intentionally
    withheld from the Skeptic (analytical judgment must not be biased by
    investigative provenance) but is visible to downstream content tooling
    (provenance informs authentic content framing). Deliberate integrity-firewall
    design.
    """

    claim: str
    primary_metric: str
    stratification: list[str]
    minimum_sample_per_cell: int
    canonicalization: CanonicalizationManifest
    motivation: str
    comparison_groups: list[str] | None = None
    expected_direction: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Hypothesis:
        return cls(
            claim=d["claim"],
            primary_metric=d["primary_metric"],
            stratification=d["stratification"],
            minimum_sample_per_cell=d["minimum_sample_per_cell"],
            canonicalization=CanonicalizationManifest.from_dict(d["canonicalization"]),
            motivation=d["motivation"],
            comparison_groups=d.get("comparison_groups"),
            expected_direction=d.get("expected_direction"),
        )


class AnalysisMethod(Enum):
    # System is SQL-only now; other members anticipate a future Python compute
    # surface (equity calc, clustering, distribution modeling).
    SQL_ONLY = "SQL_ONLY"
    PYTHON_COMPUTE = "PYTHON_COMPUTE"
    SQL_PLUS_PYTHON = "SQL_PLUS_PYTHON"


@dataclass(frozen=True)
class CellResult:
    """One stratification cell from an analysis."""

    dimensions: dict[str, str]
    point_estimate: float
    ci_low: float
    ci_high: float
    sample_size: int
    below_min_sample: bool

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CellResult:
        return cls(
            dimensions=d["dimensions"],
            point_estimate=d["point_estimate"],
            ci_low=d["ci_low"],
            ci_high=d["ci_high"],
            sample_size=d["sample_size"],
            below_min_sample=d["below_min_sample"],
        )


@dataclass(frozen=True)
class ResultsObject:
    """Analyst + Statistician output."""

    cells: list[CellResult]
    total_sample_size: int
    method: AnalysisMethod
    query_metadata: dict[str, Any]
    cross_group_comparisons: list[dict[str, Any]] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ResultsObject:
        return cls(
            cells=[CellResult.from_dict(c) for c in d["cells"]],
            total_sample_size=d["total_sample_size"],
            method=AnalysisMethod(d["method"]),
            query_metadata=d["query_metadata"],
            cross_group_comparisons=d.get("cross_group_comparisons"),
            error=d.get("error"),
        )


class VerdictOutcome(Enum):
    APPROVED = "APPROVED"
    REVISE = "REVISE"
    APPROVED_WITH_CAVEATS = "APPROVED_WITH_CAVEATS"


@dataclass(frozen=True)
class RevisionRequest:
    target: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RevisionRequest:
        return cls(target=d["target"], reason=d["reason"])


@dataclass(frozen=True)
class SkepticVerdict:
    """
    Output of the Skeptic agent.

    The Skeptic never reads Hypothesis.motivation — analytical judgment must
    not be biased by investigative provenance.
    """

    outcome: VerdictOutcome
    caveats: list[str]
    revision_requests: list[RevisionRequest]

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SkepticVerdict:
        return cls(
            outcome=VerdictOutcome(d["outcome"]),
            caveats=d["caveats"],
            revision_requests=[RevisionRequest.from_dict(r) for r in d["revision_requests"]],
        )


@dataclass(frozen=True)
class FindingProvenance:
    created_at: datetime
    motivation: str
    method: AnalysisMethod

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FindingProvenance:
        return cls(
            created_at=datetime.fromisoformat(d["created_at"]),
            motivation=d["motivation"],
            method=AnalysisMethod(d["method"]),
        )


@dataclass(frozen=True)
class PublishedFinding:
    """
    Medium-agnostic, general-purpose final artifact.

    Consumed by multiple downstream consumers (content factory,
    hand-history contextualizer, external research). Must NOT contain
    medium-specific fields (no video/blog/shot-list fields).

    `interpretation` prose: normative language ("should"/"correct"/"mistake")
    is forbidden; enforcement is upstream.
    """

    finding_id: str
    hypothesis: Hypothesis
    results: ResultsObject
    interpretation: str
    caveats: list[str]
    provenance: FindingProvenance

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PublishedFinding:
        return cls(
            finding_id=d["finding_id"],
            hypothesis=Hypothesis.from_dict(d["hypothesis"]),
            results=ResultsObject.from_dict(d["results"]),
            interpretation=d["interpretation"],
            caveats=d["caveats"],
            provenance=FindingProvenance.from_dict(d["provenance"]),
        )
