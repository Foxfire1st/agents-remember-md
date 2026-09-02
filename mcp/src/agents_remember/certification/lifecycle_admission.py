"""Exact-candidate admission and prior-red corrective authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Never

from pydantic import BaseModel

from agents_remember.certification.certificate_admission import (
    admitted_gate_identity,
    compile_certification_admission,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    CreationProvenance,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.lifecycle_models import (
    CorrectiveInputChange,
    ExactCandidateObservation,
    LifecycleAdmissionManifest,
    LifecycleAdmissionSemanticEnvelope,
    PriorRedDispositionManifest,
    PriorRedDispositionSemanticEnvelope,
    RedCatalogDisposition,
)
from agents_remember.certification.models import (
    CanonicalRailRegistry,
    CertificationPlan,
    GateResultManifest,
    RailResult,
)
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    RepositoryProfilePlan,
)
from agents_remember.errors import CertificationContractError


@dataclass(frozen=True)
class CompiledLifecycleAdmission:
    """The lifecycle boundary plus the exact owner objects it admitted."""

    lifecycle: LifecycleAdmissionManifest
    certification: CertificationAdmissionManifest
    priorRedDisposition: PriorRedDispositionManifest | None


@dataclass(frozen=True)
class LifecycleAdmissionAuthorities:
    """Mandatory owner-produced authorities; no member is discovered or defaulted."""

    registry: CanonicalRailRegistry
    certificationPlan: CertificationPlan
    repositoryProfile: CanonicalRepositoryCertificationProfile
    repositoryPlan: RepositoryProfilePlan
    candidate: ExactCandidateObservation


@dataclass(frozen=True)
class PriorRedAdmissionContext:
    priorAdmission: CertificationAdmissionManifest
    priorCatalog: GateResultManifest
    dispositions: tuple[RedCatalogDisposition, ...]


@dataclass(frozen=True)
class _DispositionContext:
    required: dict[str, RailResult]
    supplied: dict[str, RedCatalogDisposition]
    prior: CertificationAdmissionManifest
    successor: CertificationAdmissionManifest
    gate: int


@dataclass(frozen=True)
class _RefusalEvidence:
    detail: str
    expected: object
    observed: object


def compile_lifecycle_admission(
    authorities: LifecycleAdmissionAuthorities,
    *,
    provenance: CreationProvenance,
    prior_red: PriorRedAdmissionContext | None = None,
) -> CompiledLifecycleAdmission:
    """Admit one supplied authority set without discovery, execution, or mutation."""

    _require_candidate_authority(authorities.candidate)
    certification = compile_certification_admission(
        authorities.registry,
        authorities.certificationPlan,
        authorities.repositoryProfile,
        authorities.repositoryPlan,
        provenance=provenance,
    )
    _require_candidate_alignment(authorities.candidate, certification)
    prior = _compile_prior_red_disposition(
        prior_red,
        certification,
        provenance,
    )
    envelope = LifecycleAdmissionSemanticEnvelope(
        candidate=authorities.candidate,
        certificationAdmissionDigest=certification.admissionDigest,
        registryDigest=authorities.registry.registryDigest,
        certificationPlanDigest=authorities.certificationPlan.planDigest,
        repositoryProfileDigest=authorities.repositoryProfile.profileDigest,
        repositoryPlanDigest=authorities.repositoryPlan.planDigest,
        profileId=authorities.certificationPlan.profileId,
        priorRedDispositionDigest=(prior.dispositionDigest if prior is not None else None),
    )
    lifecycle = LifecycleAdmissionManifest(
        semanticEnvelope=envelope,
        admissionDigest=content_digest(envelope),
        provenance=provenance,
    )
    return CompiledLifecycleAdmission(lifecycle, certification, prior)


def validate_lifecycle_admission_currentness(
    expected: CompiledLifecycleAdmission,
    authorities: LifecycleAdmissionAuthorities,
    prior_red: PriorRedAdmissionContext | None = None,
) -> CompiledLifecycleAdmission:
    """Recompile exact current inputs and refuse movement before a gate or write."""

    observed = compile_lifecycle_admission(
        authorities,
        provenance=expected.lifecycle.provenance,
        prior_red=prior_red,
    )
    if observed != expected:
        _refuse(
            "lifecycle admission is stale",
            "lifecycle-admission-mismatch",
            "lifecycleAdmission",
            _RefusalEvidence(
                "candidate, authority, profile, plan, or prior-red corrective input moved",
                {"admissionDigest": expected.lifecycle.admissionDigest},
                {"admissionDigest": observed.lifecycle.admissionDigest},
            ),
        )
    return observed


def _require_candidate_authority(candidate: ExactCandidateObservation) -> None:
    statuses = {
        "mutationAuthorityStatus": candidate.mutationAuthorityStatus,
        "sourceAuthorityStatus": candidate.sourceAuthorityStatus,
        "branchAuthorityStatus": candidate.branchAuthorityStatus,
    }
    invalid = {key: value for key, value in statuses.items() if value != "valid"}
    if invalid:
        _refuse(
            "exact-candidate admission refused",
            "candidate-authority-invalid",
            "candidate.authority",
            _RefusalEvidence(
                "mutation, source, and branch authority must all be valid",
                {key: "valid" for key in statuses},
                statuses,
            ),
        )
    if candidate.worktreeStatus != "admissible":
        _refuse(
            "exact-candidate admission refused",
            "candidate-worktree-invalid",
            "candidate.worktreeStatus",
            _RefusalEvidence(
                "the owner-produced worktree observation is conflicted or violates its rules",
                {"worktreeStatus": "admissible", "conflictedPaths": []},
                {
                    "worktreeStatus": candidate.worktreeStatus,
                    "conflictedPaths": list(candidate.conflictedPaths),
                },
            ),
        )


def _require_candidate_alignment(
    candidate: ExactCandidateObservation,
    certification: CertificationAdmissionManifest,
) -> None:
    expected = {
        "repositoryId": candidate.repositoryId,
        "candidateCodeTree": candidate.candidateCodeTree.model_dump(mode="json"),
    }
    observed = {
        "repositoryId": certification.semanticEnvelope.repositoryId,
        "candidateCodeTree": certification.semanticEnvelope.candidateCodeTree.model_dump(
            mode="json"
        ),
    }
    if observed != expected:
        _refuse(
            "exact-candidate admission refused",
            "candidate-plan-identity-mismatch",
            "candidate",
            _RefusalEvidence(
                "the lifecycle candidate and compiled R11/R22 plan name different authority",
                expected,
                observed,
            ),
        )


def _compile_prior_red_disposition(
    prior_red: PriorRedAdmissionContext | None,
    successor: CertificationAdmissionManifest,
    provenance: CreationProvenance,
) -> PriorRedDispositionManifest | None:
    if prior_red is None:
        return None
    if not prior_red.dispositions:
        _refuse(
            "prior-red successor admission refused",
            "prior-red-authority-incomplete",
            "priorRed",
            _RefusalEvidence(
                "prior admission, red catalog, and dispositions are one indivisible authority",
                {"dispositions": "non-empty"},
                {"dispositions": "empty"},
            ),
        )
    prior_admission = prior_red.priorAdmission
    prior_catalog = prior_red.priorCatalog
    dispositions = prior_red.dispositions
    _require_prior_catalog_authority(prior_admission, prior_catalog)
    required = {
        result.rail.key: result
        for result in prior_catalog.railResults
        if result.status in {"fail", "blocked"}
    }
    supplied_by_rail = {item.rail.key: item for item in dispositions}
    if set(supplied_by_rail) != set(required):
        _refuse(
            "prior-red successor admission refused",
            "prior-red-disposition-catalog-incomplete",
            "priorRed.dispositions",
            _RefusalEvidence(
                "every failed or blocked catalog member requires exactly one corrective disposition",
                {"rails": sorted(required)},
                {"rails": sorted(supplied_by_rail)},
            ),
        )
    context = _DispositionContext(
        required,
        supplied_by_rail,
        prior_admission,
        successor,
        prior_catalog.gate,
    )
    for key, result in required.items():
        _require_result_disposition(result, supplied_by_rail[key], context)
    ordered = tuple(sorted(dispositions, key=lambda item: item.rail.key))
    envelope = PriorRedDispositionSemanticEnvelope(
        priorAdmissionDigest=prior_admission.admissionDigest,
        priorCatalogDigest=prior_catalog.manifestDigest,
        gate=prior_catalog.gate,
        successorCandidateCodeTree=successor.semanticEnvelope.candidateCodeTree,
        successorCertificationPlanDigest=successor.semanticEnvelope.certificationPlanDigest,
        dispositions=ordered,
    )
    return PriorRedDispositionManifest(
        semanticEnvelope=envelope,
        dispositionDigest=content_digest(envelope),
        provenance=provenance,
    )


def _require_prior_catalog_authority(
    admission: CertificationAdmissionManifest,
    catalog: GateResultManifest,
) -> None:
    gate = admitted_gate_identity(admission, catalog.gate)
    expected = (
        admission.semanticEnvelope.candidateCodeTree,
        admission.semanticEnvelope.profileId,
        admission.semanticEnvelope.registryDigest,
        admission.semanticEnvelope.certificationPlanDigest,
        gate.gatePlanDigest,
        "certifying",
        "red",
    )
    observed = (
        catalog.candidateIdentity,
        catalog.profileId,
        catalog.registryDigest,
        catalog.certificationPlanDigest,
        catalog.gatePlanDigest,
        catalog.altitude,
        catalog.disposition,
    )
    if observed != expected:
        _refuse(
            "prior-red successor admission refused",
            "prior-red-catalog-authority-mismatch",
            "priorRed.catalog",
            _RefusalEvidence(
                "only the exact certifying red catalog from the prior admission may authorize repair",
                {"identity": _json_identity(expected)},
                {"identity": _json_identity(observed)},
            ),
        )


def _require_result_disposition(
    result: RailResult,
    disposition: RedCatalogDisposition,
    context: _DispositionContext,
) -> None:
    expected = (result.rail, result.status, result.resultDigest, result.correctiveOwner)
    observed = (
        disposition.rail,
        disposition.priorStatus,
        disposition.priorResultDigest,
        disposition.correctiveOwner,
    )
    if observed != expected:
        _refuse(
            "prior-red successor admission refused",
            "prior-red-disposition-identity-mismatch",
            f"priorRed.dispositions.{result.rail.key}",
            _RefusalEvidence(
                "corrective disposition does not bind the exact failed or blocked rail result",
                {"identity": _json_identity(expected)},
                {"identity": _json_identity(observed)},
            ),
        )
    if disposition.disposition == "repaired-root":
        root = disposition.repairedRoot
        assert root is not None
        blockers = {item.key for item in result.blockedBy}
        root_result = context.required.get(root.key)
        root_disposition = context.supplied.get(root.key)
        if (
            result.status != "blocked"
            or root.key not in blockers
            or root_result is None
            or root_result.status != "fail"
            or root_disposition is None
            or root_disposition.disposition != "direct-repair"
        ):
            _refuse(
                "prior-red successor admission refused",
                "prior-red-repaired-root-invalid",
                f"priorRed.dispositions.{result.rail.key}.repairedRoot",
                _RefusalEvidence(
                    "a blocked dependant may cite only its directly failed, directly repaired root",
                    {"failedDirectBlockers": sorted(blockers)},
                    {"repairedRoot": root.key},
                ),
            )
        return
    for change in disposition.changedInputs:
        _require_relevant_change(
            change,
            context.prior,
            context.successor,
            context.gate,
            disposition.rail.key,
        )


def _require_relevant_change(
    change: CorrectiveInputChange,
    prior: CertificationAdmissionManifest,
    successor: CertificationAdmissionManifest,
    gate: int,
    rail_key: str,
) -> None:
    if change.key == ("candidate-code-tree", "candidate"):
        expected = prior.semanticEnvelope.candidateCodeTree.value
        observed = successor.semanticEnvelope.candidateCodeTree.value
    else:
        prior_inputs = {
            item.key: item.contentDigest
            for item in admitted_gate_identity(prior, gate).semanticInputs
        }
        successor_inputs = {
            item.key: item.contentDigest
            for item in admitted_gate_identity(successor, gate).semanticInputs
        }
        expected = prior_inputs.get(change.key)
        observed = successor_inputs.get(change.key)
    if expected != change.beforeDigest or observed != change.afterDigest:
        _refuse(
            "prior-red successor admission refused",
            "prior-red-changed-input-unproven",
            f"priorRed.dispositions.{rail_key}.changedInputs",
            _RefusalEvidence(
                "the corrective change is not an exact changed input of the prior and successor gate",
                {
                    "inputKind": change.inputKind,
                    "inputId": change.inputId,
                    "beforeDigest": expected,
                    "afterDigest": observed,
                },
                change.model_dump(mode="json"),
            ),
        )


def _json_identity(values: tuple[object, ...]) -> list[object]:
    return [
        value.model_dump(mode="json") if isinstance(value, BaseModel) else value for value in values
    ]


def _refuse(
    detail: str,
    code: str,
    path: str,
    evidence: _RefusalEvidence,
) -> Never:
    raise CertificationContractError(
        detail,
        (
            {
                "code": code,
                "path": path,
                "detail": evidence.detail,
                "expected": evidence.expected,
                "observed": evidence.observed,
                "gateStarts": 0,
            },
        ),
    )


__all__ = [
    "CompiledLifecycleAdmission",
    "LifecycleAdmissionAuthorities",
    "PriorRedAdmissionContext",
    "compile_lifecycle_admission",
    "validate_lifecycle_admission_currentness",
]
