"""Exact green Gate 1-4 prerequisite adapter for the final Gate-5 certification.

CCR-R08 requires that no full memory scan or coherence publication starts before the
exact candidate's Gate 1-4 certificates are green and current. This adapter re-observes
the R21 chain and reuse plan for one exact code candidate and refuses any stale,
incomplete, or invalidated prefix with a typed Gate-5 refusal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    GateCertificate,
    GateCertificateIdentity,
)
from agents_remember.certification.digests import content_digest
from agents_remember.errors import CertificationContractError, FinalCertificationError


@dataclass(frozen=True)
class GateFourPrefixProof:
    """The exact reusable green Gate 1-4 prefix for one candidate."""

    reusedGateOneToFour: tuple[GateCertificateIdentity, ...]
    certificateReusePlanDigest: str


def require_green_gate_prefix(
    admission: CertificationAdmissionManifest,
    certificates: Sequence[GateCertificate],
    changes: Sequence[CertificateInputChange],
    *,
    code_tree: str,
) -> GateFourPrefixProof:
    """Admit one exact current green Gate 1-4 prefix or refuse with a typed refusal.

    The caller holds the exact prefix chain. plan_certificate_reuse is the single R21
    authority deciding what may be reused; the adapter only allows the memory-only Gate-5
    start (reuse exactly Gates 1-4, first gate to run is 5) for the exact admitted code
    candidate. Any earlier invalidation -- including a code change, which invalidates
    Gate 5 and restarts at Gate 1 -- refuses before any memory scan or coherence
    publication.
    """

    ordered = tuple(certificates)
    if tuple(item.semanticEnvelope.gate for item in ordered) != (1, 2, 3, 4):
        raise FinalCertificationError(
            "gate-five-prefix-incomplete",
            "final full memory certification requires the exact ordered green Gate 1-4 prefix",
            expected={"certificateGates": (1, 2, 3, 4)},
            observed={"certificateGates": tuple(item.semanticEnvelope.gate for item in ordered)},
            next_action="worktree_closeout",
        )
    admitted_code_tree = admission.semanticEnvelope.candidateCodeTree.value
    if admitted_code_tree != code_tree:
        raise FinalCertificationError(
            "gate-five-code-candidate-mismatch",
            "the certified code candidate is not the exact admitted code tree; "
            "a code change restarts certification at Gate 1",
            expected={"candidateCodeTree": admitted_code_tree},
            observed={"candidateCodeTree": code_tree},
            next_action="worktree_closeout",
        )
    try:
        validate_certificate_chain(admission, ordered, gate_five_inputs=None)
    except CertificationContractError as error:
        raise FinalCertificationError(
            "gate-five-prefix-stale",
            "the exact green Gate 1-4 certificate prefix is stale against the current admission",
            expected={"state": "current-green-1-4"},
            observed={"refusal": _refusal_summary(error)},
            next_action="worktree_closeout",
        ) from error
    try:
        reuse = plan_certificate_reuse(admission, ordered, changes)
    except (CertificationContractError, ValueError) as error:
        raise FinalCertificationError(
            "gate-five-prefix-invalidated",
            "R21 refused the Gate 1-4 prefix for the final memory certification",
            expected={"firstGateToRun": 5},
            observed={"refusal": _refusal_summary(error)},
            next_action="worktree_closeout",
        ) from error
    identities = tuple(item.identity for item in ordered)
    if (
        reuse.reusedCertificates != identities
        or reuse.firstGateToRun != 5
        or reuse.invalidatedGates != (5,)
        or reuse.zeroGateStarts
    ):
        raise FinalCertificationError(
            "gate-five-prefix-invalidated",
            "a changed earlier gate invalidated the certified code candidate; "
            "the candidate must restart at Gate 1 instead of being repaired as memory-only",
            expected={
                "reusedCertificates": identities,
                "firstGateToRun": 5,
                "invalidatedGates": (5,),
            },
            observed={
                "reusedCertificates": reuse.reusedCertificates,
                "firstGateToRun": reuse.firstGateToRun,
                "invalidatedGates": reuse.invalidatedGates,
                "zeroGateStarts": reuse.zeroGateStarts,
            },
            next_action="worktree_closeout",
        )
    return GateFourPrefixProof(
        reusedGateOneToFour=identities,
        certificateReusePlanDigest=content_digest(reuse.model_dump(mode="json")),
    )


def _refusal_summary(error: Exception) -> str:
    findings = getattr(error, "findings", ())
    if findings:
        return str(findings[0].get("code") or findings[0].get("detail") or str(error))
    return str(error)


__all__ = ["GateFourPrefixProof", "require_green_gate_prefix"]
