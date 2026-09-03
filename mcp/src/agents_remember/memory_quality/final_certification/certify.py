"""Executable surface of the final full memory-coherence certification (CCR-R08 Gate 5).

certify_final_full_memory_coherence runs the complete Gate-5 protocol against one
exact candidate pair: prove the green Gate 1-4 prefix, bind the exact memory tree after
governed writes, run the complete final catalog with content-addressed subresults,
require the current canonical coherence record and exact candidate pair, assemble the
R21 Gate-5 semantic inputs, and publish one typed green/red/blocked certification.
Certification never mutates code or memory; every refusal is typed and blocks
finalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from agents_remember.certification.certificate_invalidation import CertificateInputChange
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    GateCertificate,
)
from agents_remember.errors import FinalCertificationError
from agents_remember.memory_quality.incremental_scope.affected_models import (
    AffectedClosurePlan,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    ValidatedCuratorCoherence,
)

from .catalog import (
    ExecutedFinalCatalog,
    compile_final_catalog_plan,
    final_catalog_attestation,
)
from .certificate import assemble_gate_five_inputs, coherence_subrecords
from .gate_prefix import require_green_gate_prefix
from .models import FinalCertificationResult, FinalItemStatus


@dataclass(frozen=True)
class FinalCertificationEvidence:
    """Every exact authority the final certification may read; nothing is inferred."""

    admission: CertificationAdmissionManifest
    gate_certificates: tuple[GateCertificate, ...]
    changes: tuple[CertificateInputChange, ...]
    candidate_code_tree: str
    memory_tree: str
    pair_identity: MemoryCandidatePairIdentity
    affected_closure: AffectedClosurePlan
    coherence: ValidatedCuratorCoherence | None
    executed_checks: dict[str, dict[str, object]]
    missing_onboarding_count: int
    stale_route_index_count: int
    full_only_rerun: bool


def certify_final_full_memory_coherence(
    evidence: FinalCertificationEvidence,
) -> FinalCertificationResult:
    """Certify one exact candidate pair through the complete Gate-5 catalog."""

    proof = require_green_gate_prefix(
        evidence.admission,
        evidence.gate_certificates,
        evidence.changes,
        code_tree=evidence.candidate_code_tree,
    )
    if evidence.coherence is None:
        _refuse(
            "gate-five-coherence-blocked",
            "final full memory certification requires the current canonical "
            "curator-coherence authority",
            next_action="curator_coherence",
        )
    record = evidence.coherence.record
    record_digest = evidence.coherence.record_digest
    subrecords = coherence_subrecords(
        record_digest=record_digest,
        record=record,
        affected_subrecords=evidence.affected_closure.affectedCoherenceSubrecords,
    )
    plan = compile_final_catalog_plan(
        candidate_code_tree=evidence.candidate_code_tree,
        memory_tree=evidence.memory_tree,
        affected_closure=evidence.affected_closure,
        coherence_subrecords=subrecords,
        candidate_pair_authority_digest=evidence.pair_identity.contractDigest,
    )
    attestation = final_catalog_attestation(
        plan,
        ExecutedFinalCatalog(
            executed_checks=evidence.executed_checks,
            missing_onboarding_count=evidence.missing_onboarding_count,
            stale_route_index_count=evidence.stale_route_index_count,
            affected_closure_status=_affected_closure_status(evidence.full_only_rerun),
            coherence_status="pass",
            pair_status="pass",
            full_only_rerun=evidence.full_only_rerun,
            coherence_record_digest=record_digest,
        ),
    )
    gate_five_inputs = (
        assemble_gate_five_inputs(
            memory_tree=plan.memoryTree,
            affected_closure_plan_digest=plan.affectedClosurePlanDigest,
            checker_registry_digest=plan.checkerRegistryDigest,
            coherence_subrecords=plan.coherenceSubrecords,
            candidate_pair_authority_digest=plan.candidatePairAuthorityDigest,
        )
        if attestation.ok
        else None
    )
    if attestation.ok:
        state = "green"
        reason = None
    elif attestation.statusCounts["fail"]:
        state = "red"
        reason = "at least one final catalog item failed"
    else:
        state = "blocked"
        reason = "the final catalog is blocked and cannot certify"
    return FinalCertificationResult(
        state=state,
        reason=reason,
        plan=plan,
        attestation=attestation,
        gateFiveInputs=gate_five_inputs,
        coherenceRecordDigest=record_digest,
        reusedGateOneToFour=proof.reusedGateOneToFour,
        certificateReusePlanDigest=proof.certificateReusePlanDigest,
        finalizationEligible=state == "green",
    )


def _affected_closure_status(full_only_rerun: bool) -> FinalItemStatus:
    """The affected-closure item blocks until the repair loop consumed R07's
    pending-final-full population through the complete final catalog."""

    return "pass" if full_only_rerun else "blocked"


def _refuse(status: str, detail: str, *, next_action: str) -> NoReturn:
    raise FinalCertificationError(status, detail, next_action=next_action)


__all__ = ["FinalCertificationEvidence", "certify_final_full_memory_coherence"]
