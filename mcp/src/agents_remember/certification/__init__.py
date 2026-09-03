"""Repository-neutral contracts for five-gate closeout certification."""

from agents_remember.certification.canonical import canonicalize_registry
from agents_remember.certification.certificate_admission import (
    compile_certification_admission,
)
from agents_remember.certification.certificate_authority import (
    compile_finalization_authority,
    compile_gate_certificate,
    validate_certificate_chain,
    validate_finalization_currentness,
)
from agents_remember.certification.certificate_invalidation import (
    classify_certificate_invalidation,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_store import ContentAddressedCertificateStore
from agents_remember.certification.lifecycle_admission import (
    compile_lifecycle_admission,
    validate_lifecycle_admission_currentness,
)
from agents_remember.certification.lifecycle_recovery import (
    authorize_finalization_leg,
    compile_certification_recovery_record,
    compile_lifecycle_finalization,
    validate_lifecycle_finalization_currentness,
)
from agents_remember.certification.planning import (
    admit_certification_plan,
    compile_certification_plan,
)
from agents_remember.certification.readiness import (
    READINESS_SURFACES,
    compile_closeout_readiness,
    project_closeout_readiness,
    readiness_projection_bytes,
)
from agents_remember.certification.readiness_models import (
    CloseoutReadinessInput,
    CloseoutReadinessProjection,
    DiagnosticReadinessObservation,
    GateReadinessObservation,
    LifecycleReadinessObservation,
    ProfileReadinessObservation,
    ReadinessEvidenceReference,
    ReadinessRevision,
)
from agents_remember.certification.readiness_transitions import (
    CANONICAL_READINESS_TRANSITIONS,
    require_readiness_transition,
)
from agents_remember.certification.repository_profiles import (
    admit_repository_profile_plan,
    canonicalize_repository_profile,
    compile_repository_profile_plan,
    load_repository_profile,
    validate_repository_profile,
)
from agents_remember.certification.results import (
    build_rail_result,
    compile_gate_result_manifest,
)
from agents_remember.certification.validation import validate_registry

__all__ = [
    "CANONICAL_READINESS_TRANSITIONS",
    "READINESS_SURFACES",
    "CloseoutReadinessInput",
    "CloseoutReadinessProjection",
    "ContentAddressedCertificateStore",
    "DiagnosticReadinessObservation",
    "GateReadinessObservation",
    "LifecycleReadinessObservation",
    "ProfileReadinessObservation",
    "ReadinessEvidenceReference",
    "ReadinessRevision",
    "admit_certification_plan",
    "admit_repository_profile_plan",
    "authorize_finalization_leg",
    "build_rail_result",
    "canonicalize_registry",
    "canonicalize_repository_profile",
    "classify_certificate_invalidation",
    "compile_certification_admission",
    "compile_certification_plan",
    "compile_certification_recovery_record",
    "compile_closeout_readiness",
    "compile_finalization_authority",
    "compile_gate_certificate",
    "compile_gate_result_manifest",
    "compile_lifecycle_admission",
    "compile_lifecycle_finalization",
    "compile_repository_profile_plan",
    "load_repository_profile",
    "plan_certificate_reuse",
    "project_closeout_readiness",
    "readiness_projection_bytes",
    "require_readiness_transition",
    "validate_certificate_chain",
    "validate_finalization_currentness",
    "validate_lifecycle_admission_currentness",
    "validate_lifecycle_finalization_currentness",
    "validate_registry",
    "validate_repository_profile",
]
