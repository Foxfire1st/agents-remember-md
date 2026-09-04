"""CCR-R13: one optional non-certifying diagnostic E2E lane.

The package owns the durable diagnostic manifest, the diagnostic-altitude plan
projection, the optional-lane readiness projection, and the nonce/telemetry
identity helpers.  Run control that binds the exact R12 host runner/store
authority lives at the higher worktree quality layer
(agents_remember.worktrees.modules.quality.diagnostic_executor), which
consumes these contracts through the trusted R12 launcher.
"""

from agents_remember.certification.diagnostics.models import (
    DiagnosticArtifact,
    DiagnosticAttemptRecord,
    DiagnosticAttemptState,
    DiagnosticDisposition,
    DiagnosticEnvironmentBinding,
    DiagnosticFailureClass,
    DiagnosticFailureRecord,
    DiagnosticPlanRecord,
    DiagnosticRunManifest,
    DiagnosticRunResult,
    DiagnosticRunResultDraft,
    DiagnosticRuntimeAuthorityBinding,
    DiagnosticTeardownRecord,
)
from agents_remember.certification.diagnostics.planning import (
    compile_diagnostic_plan,
    diagnostic_scenario_gate,
    scenario_gate_digest,
)
from agents_remember.certification.diagnostics.projection import (
    DiagnosticLaneDisposition,
    DiagnosticLaneProjection,
    diagnostic_blocks_certification,
    diagnostic_never_satisfies_certification,
    project_diagnostic_lane,
)
from agents_remember.certification.diagnostics.store import (
    DiagnosticManifestStore,
    DiagnosticStorePolicy,
)

__all__ = [
    "DiagnosticArtifact",
    "DiagnosticAttemptRecord",
    "DiagnosticAttemptState",
    "DiagnosticDisposition",
    "DiagnosticEnvironmentBinding",
    "DiagnosticFailureClass",
    "DiagnosticFailureRecord",
    "DiagnosticLaneDisposition",
    "DiagnosticLaneProjection",
    "DiagnosticManifestStore",
    "DiagnosticPlanRecord",
    "DiagnosticRunManifest",
    "DiagnosticRunResult",
    "DiagnosticRunResultDraft",
    "DiagnosticRuntimeAuthorityBinding",
    "DiagnosticStorePolicy",
    "DiagnosticTeardownRecord",
    "compile_diagnostic_plan",
    "diagnostic_blocks_certification",
    "diagnostic_never_satisfies_certification",
    "diagnostic_scenario_gate",
    "project_diagnostic_lane",
    "scenario_gate_digest",
]
