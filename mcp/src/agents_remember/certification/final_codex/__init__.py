"""CCR-R14: the final real-Codex Gate-4 certification lane.

The package owns the immutable two-fresh no-retry certifying repetition
vocabulary (models), the plan record compiler and exact-predecessor barriers
(planning), the durable CAS run store (store), the certificate-readiness
projection (projection), and the bound Gate-4 certificate compiler
(certificate).  Run control that binds the exact R12 host runner/store
authority lives at the higher worktree quality layer
(agents_remember.worktrees.modules.quality.final_codex_executor), which
consumes these contracts through the trusted R12 launcher.
"""

from agents_remember.certification.final_codex.certificate import (
    FinalCodexCertificateEnvelope,
    FinalCodexGateFourCertificate,
    compile_gate_four_certificate,
)
from agents_remember.certification.final_codex.models import (
    CERTIFYING_GATE,
    REPETITION_COUNT,
    FinalCodexArtifact,
    FinalCodexAttemptRecord,
    FinalCodexAttemptState,
    FinalCodexDisposition,
    FinalCodexEnvironmentBinding,
    FinalCodexFailureClass,
    FinalCodexFailureRecord,
    FinalCodexPlanRecord,
    FinalCodexRepetitionIdentity,
    FinalCodexRepetitionResult,
    FinalCodexRepetitionResultDraft,
    FinalCodexRunManifest,
    FinalCodexRuntimeAuthorityBinding,
    FinalCodexTeardownRecord,
)
from agents_remember.certification.final_codex.planning import (
    compile_final_codex_plan_record,
    final_codex_gate_plan,
    require_gates_one_to_three_green,
)
from agents_remember.certification.final_codex.projection import (
    FinalCodexLaneDisposition,
    FinalCodexLaneProjection,
    final_codex_certificate_ready,
    project_final_codex_lane,
)
from agents_remember.certification.final_codex.store import (
    FinalCodexManifestStore,
    FinalCodexStorePolicy,
)

__all__ = [
    "CERTIFYING_GATE",
    "REPETITION_COUNT",
    "FinalCodexArtifact",
    "FinalCodexAttemptRecord",
    "FinalCodexAttemptState",
    "FinalCodexCertificateEnvelope",
    "FinalCodexDisposition",
    "FinalCodexEnvironmentBinding",
    "FinalCodexFailureClass",
    "FinalCodexFailureRecord",
    "FinalCodexGateFourCertificate",
    "FinalCodexLaneDisposition",
    "FinalCodexLaneProjection",
    "FinalCodexManifestStore",
    "FinalCodexPlanRecord",
    "FinalCodexRepetitionIdentity",
    "FinalCodexRepetitionResult",
    "FinalCodexRepetitionResultDraft",
    "FinalCodexRunManifest",
    "FinalCodexRuntimeAuthorityBinding",
    "FinalCodexStorePolicy",
    "FinalCodexTeardownRecord",
    "compile_final_codex_plan_record",
    "compile_gate_four_certificate",
    "final_codex_certificate_ready",
    "final_codex_gate_plan",
    "project_final_codex_lane",
    "require_gates_one_to_three_green",
]
