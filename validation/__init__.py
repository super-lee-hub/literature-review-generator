from .evidence_resolver import (
    EvidenceCandidate,
    EvidenceResolver,
    EvidenceResolverContext,
    build_evidence_resolver_context,
)

from .evidence_loader import (
    PreprocessEvidence,
    PreprocessEvidenceLoader,
    build_evidence_context_from_preprocess,
)
from validation.review_validator import (
    CitationValidationResult,
    ReviewValidationReport,
    ReviewValidator,
    RootCause,
    ValidationConclusion,
)
from validation.summary_recheck import (
    SummaryCorrectionCandidate,
    SummaryRecheckReport,
    SummaryRechecker,
    WHITELISTED_FIELDS,
    run_summary_rechecks,
)
from validation.repair_models import (
    AppliedPatchRecord,
    ApplyGuardResult,
    DependencyHashBundle,
    PatchGranularity,
    PatchProposal,
    PatchTargetSignature,
    RepairApplyResult,
    RepairPlan,
    RepairPolicy,
    RepairReport,
    RepairRootCause,
)
from validation.repair_planner import (
    RepairPlanner,
    run_repair_planning,
)
from validation.repair_apply import (
    RepairApplier,
    check_apply_guards,
    run_repair_apply,
)

__all__ = [
    # Week 3 validation
    "EvidenceCandidate",
    "EvidenceResolver",
    "EvidenceResolverContext",
    "build_evidence_resolver_context",
    "PreprocessEvidence",
    "PreprocessEvidenceLoader",
    "build_evidence_context_from_preprocess",
    "CitationValidationResult",
    "ReviewValidationReport",
    "ReviewValidator",
    "RootCause",
    "ValidationConclusion",
    "SummaryCorrectionCandidate",
    "SummaryRecheckReport",
    "SummaryRechecker",
    "WHITELISTED_FIELDS",
    "run_summary_rechecks",
    # Week 4 repair pipeline
    "AppliedPatchRecord",
    "ApplyGuardResult",
    "DependencyHashBundle",
    "PatchGranularity",
    "PatchProposal",
    "PatchTargetSignature",
    "RepairApplyResult",
    "RepairPlan",
    "RepairPolicy",
    "RepairReport",
    "RepairRootCause",
    "RepairPlanner",
    "run_repair_planning",
    "RepairApplier",
    "check_apply_guards",
    "run_repair_apply",
]
