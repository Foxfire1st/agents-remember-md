"""Derive recovery inputs from retained owners before any gate can start."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Never

from agents_remember.certification.certificate_admission import admitted_gate_identity
from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    InputChangeClass,
)
from agents_remember.certification.certificate_models import GateCertificate, GateFiveSemanticInputs
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.models import FrozenCertificationRun
from agents_remember.certification.lifecycle_admission import (
    LifecycleAdmissionAuthorities,
    PriorRedAdmissionContext,
    compile_lifecycle_admission,
)
from agents_remember.certification.lifecycle_models import ExactCandidateObservation
from agents_remember.certification.models import GateResultAdmission, GateResultManifest
from agents_remember.certification.results import compile_gate_result_manifest
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId
from agents_remember.models.certification.corrective import RedCatalogDisposition

_GATE_CLASSES: dict[GateId, InputChangeClass] = {
    1: "gate-1-input",
    2: "gate-2-input",
    3: "gate-3-input",
    4: "gate-4-input",
    5: "memory-onboarding",
}
_RUNTIME_KINDS = frozenset(
    {"rail-runtime", "repository-rail-runtime", "repository-executor-adapter"}
)


@dataclass(frozen=True)
class RecoveryInputSnapshot:
    """Exact retained run plus the independently observed candidate and optional memory port."""

    run: FrozenCertificationRun
    candidate: ExactCandidateObservation
    memory_inputs: GateFiveSemanticInputs | None = None


def derive_certificate_input_changes(
    prior: RecoveryInputSnapshot,
    current: RecoveryInputSnapshot,
) -> tuple[CertificateInputChange, ...]:
    """Classify owner-produced before/after bytes; never take a caller's claimed repair class."""

    _require_snapshot(prior)
    _require_snapshot(current)
    _require_same_task(prior, current)
    changes: list[CertificateInputChange] = []
    if prior.candidate.candidateCodeTree != current.candidate.candidateCodeTree:
        changes.append(
            CertificateInputChange(
                changeClass="code",
                reason=f"Candidate code tree changed from {prior.candidate.candidateCodeTree.value} "
                f"to {current.candidate.candidateCodeTree.value}.",
            )
        )
    changes.extend(_gate_changes(prior.run, current.run))
    changes.extend(_memory_changes(prior.memory_inputs, current.memory_inputs))
    changes.extend(_candidate_changes(prior, current))
    if not changes:
        changes.append(
            CertificateInputChange(
                changeClass="unchanged-interruption",
                reason="The supplied original run and current owner inputs have no relevant change; "
                "reuse still requires the exact selected certificates and current memory inputs.",
            )
        )
    return tuple(sorted(changes, key=content_digest))


def build_prior_red_context(
    prior: FrozenCertificationRun,
    current: RecoveryInputSnapshot,
    catalog: GateResultManifest | None,
    dispositions: tuple[RedCatalogDisposition, ...],
    certificates: tuple[GateCertificate, ...],
) -> PriorRedAdmissionContext:
    """Validate the original complete red catalog and every caller-supplied corrective decision.

    Publication/readback and journal selection belong to the caller. This helper
    consumes those selected objects without searching, writing, or starting work.
    """

    _require_snapshot(current)
    if prior.admission.semanticEnvelope.repositoryId != current.candidate.repositoryId:
        _refuse(
            "prior-red-repository-mismatch",
            current.candidate.repositoryId,
            prior.admission.semanticEnvelope.repositoryId,
        )
    if catalog is None:
        _refuse("prior-red-catalog-missing", "selected complete certifying red catalog", None)
    expected_gates = tuple(range(1, catalog.gate))
    actual_gates = tuple(item.semanticEnvelope.gate for item in certificates)
    if actual_gates != expected_gates:
        _refuse("prior-red-certificate-prefix-incomplete", expected_gates, actual_gates)
    if len({item.rail.key for item in dispositions}) != len(dispositions):
        _refuse("prior-red-disposition-duplicate", "one disposition per rail", len(dispositions))
    context = PriorRedAdmissionContext(prior.admission, catalog, dispositions)
    try:
        validate_certificate_chain(prior.admission, certificates)
        _require_complete_red_catalog(prior, catalog)
        compile_lifecycle_admission(
            _authorities(current),
            provenance=current.run.provenance,
            prior_red=context,
        )
    except CertificationContractError as error:
        raise CertificationContractError(
            "prior-red recovery refused before execution",
            tuple({**finding, "gateStarts": 0} for finding in error.findings),
        ) from error
    return context


def _authorities(snapshot: RecoveryInputSnapshot) -> LifecycleAdmissionAuthorities:
    run = snapshot.run
    return LifecycleAdmissionAuthorities(
        registry=run.registry,
        certificationPlan=run.certificationPlan,
        repositoryProfile=run.repositoryProfile,
        repositoryPlan=run.repositoryPlan,
        candidate=snapshot.candidate,
    )


def _require_snapshot(snapshot: RecoveryInputSnapshot) -> None:
    compiled = compile_lifecycle_admission(
        _authorities(snapshot), provenance=snapshot.run.provenance
    )
    if compiled.certification != snapshot.run.admission:
        _refuse(
            "recovery-frozen-admission-mismatch",
            snapshot.run.admission.admissionDigest,
            compiled.certification.admissionDigest,
        )


def _require_same_task(prior: RecoveryInputSnapshot, current: RecoveryInputSnapshot) -> None:
    fields = ("taskId", "contractPath", "repositoryId", "sourceBranchRef")
    before = prior.candidate.model_dump(mode="json", include=set(fields))
    after = current.candidate.model_dump(mode="json", include=set(fields))
    if before != after:
        _refuse("recovery-candidate-address-mismatch", before, after)


def _gate_changes(
    prior: FrozenCertificationRun, current: FrozenCertificationRun
) -> tuple[CertificateInputChange, ...]:
    changes: list[CertificateInputChange] = []
    runtime_gates: list[GateId] = []
    for old in prior.admission.semanticEnvelope.gates:
        new = admitted_gate_identity(current.admission, old.gate)
        before = {item.key: item.contentDigest for item in old.semanticInputs}
        after = {item.key: item.contentDigest for item in new.semanticInputs}
        changed_keys = {
            key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
        }
        if any(key[0] in _RUNTIME_KINDS for key in changed_keys):
            runtime_gates.append(old.gate)
        code_changed = prior.admission.semanticEnvelope.candidateCodeTree != (
            current.admission.semanticEnvelope.candidateCodeTree
        )
        if changed_keys or (not code_changed and old.gateSemanticDigest != new.gateSemanticDigest):
            changes.append(
                CertificateInputChange(
                    changeClass=_GATE_CLASSES[old.gate],
                    reason=f"Gate {old.gate} retained admission changed from {content_digest(old)} "
                    f"to {content_digest(new)}; {len(changed_keys)} exact input identities changed.",
                )
            )
    if runtime_gates:
        changes.append(
            CertificateInputChange(
                changeClass="runtime-toolchain-executor-image",
                consumingGates=tuple(runtime_gates),
                reason="Changed runtime/executor identities are declared by the retained "
                f"admissions in Gates {', '.join(map(str, runtime_gates))}.",
            )
        )
    return tuple(changes)


def _memory_changes(
    prior: GateFiveSemanticInputs | None, current: GateFiveSemanticInputs | None
) -> tuple[CertificateInputChange, ...]:
    if (prior is None) != (current is None):
        _refuse(
            "recovery-memory-inputs-incomplete",
            "both exact memory observations, or neither before the memory port has run",
            {"priorProvided": prior is not None, "currentProvided": current is not None},
        )
    if prior is None or current is None:
        return ()
    changes: list[CertificateInputChange] = []
    if prior.model_dump(exclude={"coherenceSubrecords"}) != current.model_dump(
        exclude={"coherenceSubrecords"}
    ):
        changes.append(
            CertificateInputChange(
                changeClass="memory-onboarding",
                reason="The exact memory tree, affected closure, checker registry or candidate-pair "
                f"authority changed from {content_digest(prior)} to {content_digest(current)}.",
            )
        )
    before = {item.subrecordId: item.contentDigest for item in prior.coherenceSubrecords}
    after = {item.subrecordId: item.contentDigest for item in current.coherenceSubrecords}
    changed = tuple(
        sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    )
    if changed:
        changes.append(
            CertificateInputChange(
                changeClass="coherence-evidence",
                affectedGateFiveSubrecords=changed,
                reason="The supplied original and current coherence subrecord identities differ.",
            )
        )
    return tuple(changes)


def _candidate_changes(
    prior: RecoveryInputSnapshot, current: RecoveryInputSnapshot
) -> tuple[CertificateInputChange, ...]:
    changes: list[CertificateInputChange] = []
    semantic_fields = {"topologyIdentityDigest", "taskIntentIdentityDigest"}
    if prior.candidate.model_dump(include=semantic_fields) != current.candidate.model_dump(
        include=semantic_fields
    ):
        changes.append(
            CertificateInputChange(
                changeClass="topology-intent",
                reason="The observed task topology or normative intent changed; any consumed "
                "gate bytes are classified separately from their exact admitted inputs.",
            )
        )
    excluded = {*semantic_fields, "candidateCodeTree"}
    if (
        prior.candidate.model_dump(exclude=excluded)
        != current.candidate.model_dump(exclude=excluded)
        or prior.run.provenance != current.run.provenance
    ):
        changes.append(
            CertificateInputChange(
                changeClass="journal-review-approval-attempt",
                reason="Candidate authority, commit-intent or creation evidence changed; "
                "revalidate finalization without inventing a gate input change.",
            )
        )
    return tuple(changes)


def _require_complete_red_catalog(run: FrozenCertificationRun, catalog: GateResultManifest) -> None:
    if catalog.altitude != "certifying" or catalog.disposition != "red":
        _refuse(
            "prior-red-catalog-authority-mismatch",
            {"altitude": "certifying", "disposition": "red"},
            {"altitude": catalog.altitude, "disposition": catalog.disposition},
        )
    gate = next(item for item in run.certificationPlan.gates if item.gate == catalog.gate)
    rebuilt = compile_gate_result_manifest(
        run.registry,
        run.certificationPlan,
        gate,
        catalog.railResults,
        GateResultAdmission(
            candidateIdentity=run.certificationPlan.candidateIdentity,
            profileId=run.certificationPlan.profileId,
            altitude="certifying",
        ),
    )
    if rebuilt != catalog:
        _refuse("prior-red-catalog-mismatch", rebuilt.manifestDigest, catalog.manifestDigest)


def _refuse(code: str, expected: object, observed: object) -> Never:
    raise CertificationContractError(
        "certification recovery input refused before execution",
        (
            {
                "code": code,
                "path": "recovery",
                "expected": expected,
                "observed": observed,
                "gateStarts": 0,
            },
        ),
    )
