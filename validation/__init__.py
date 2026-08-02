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
from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationExecutionStatus,
    ValidationRunDisposition,
    ValidationRunResultError,
    ValidationRunResultV1,
    claim_verdict_for_result,
    reduce_validation_disposition,
)
from validation.closure import (
    VALIDATION_CLOSURE_ARTIFACT_TYPE,
    VALIDATION_CLOSURE_ARTIFACT_VERSION,
    ValidationClosureResult,
    ValidationClosureService,
    persist_validation_closure,
)
from validation.repair_transaction import (
    REPAIR_TRANSACTION_ARTIFACT_TYPE,
    REPAIR_TRANSACTION_ARTIFACT_VERSION,
    RepairTransactionRecord,
    RepairTransactionService,
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
    # Canonical validation truth source
    "ClaimValidationResultV1",
    "ClaimVerdict",
    "ValidationExecutionStatus",
    "ValidationRunDisposition",
    "ValidationRunResultError",
    "ValidationRunResultV1",
    "claim_verdict_for_result",
    "reduce_validation_disposition",
    # Canonical review-chain closure
    "VALIDATION_CLOSURE_ARTIFACT_TYPE",
    "VALIDATION_CLOSURE_ARTIFACT_VERSION",
    "ValidationClosureResult",
    "ValidationClosureService",
    "persist_validation_closure",
    "REPAIR_TRANSACTION_ARTIFACT_TYPE",
    "REPAIR_TRANSACTION_ARTIFACT_VERSION",
    "RepairTransactionRecord",
    "RepairTransactionService",
]
