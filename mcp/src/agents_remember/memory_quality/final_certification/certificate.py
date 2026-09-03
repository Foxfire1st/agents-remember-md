"""R21 Gate-5 semantic-input assembly from executed full coherence certification."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import NoReturn

from pydantic import ValidationError

from agents_remember.certification.certificate_models import (
    CoherenceSubrecordIdentity,
    GateFiveSemanticInputs,
)
from agents_remember.certification.models import CandidateIdentity
from agents_remember.errors import FinalCertificationError
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorCoherenceRecord,
)

_JUDGMENT_SUBRECORD_PREFIX = "judgment-evidence"


def coherence_subrecords(
    *,
    record_digest: str,
    record: CuratorCoherenceRecord,
    affected_subrecords: Sequence[str] = (),
) -> tuple[CoherenceSubrecordIdentity, ...]:
    """Derive the canonical coherence subrecord set from the current authority record.

    One subrecord for the immutable record itself and one content-addressed subrecord per
    judgment evidence byte range the record binds. affected_subrecords name raw
    judgment evidence references (or coherence-record); each must be covered by the
    current record so that coherence-evidence invalidation can never be hidden behind a
    stale repair plan.
    """

    covered_refs = {judgment.evidenceRef for judgment in record.judgments}
    missing_affected = sorted(
        reference
        for reference in affected_subrecords
        if reference != "coherence-record" and reference not in covered_refs
    )
    if missing_affected:
        raise FinalCertificationError(
            "gate-five-affected-coherence-subrecords-uncovered",
            "repair-declared affected coherence subrecords are not covered by the current record",
            expected={"affectedSubrecords": sorted(set(affected_subrecords))},
            observed={"uncoveredSubrecords": missing_affected},
            next_action="curator_coherence",
        )
    subrecords = [
        CoherenceSubrecordIdentity(
            subrecordId="coherence-record",
            contentDigest=record_digest,
        )
    ]
    subrecords.extend(
        CoherenceSubrecordIdentity(
            subrecordId=_judgment_subrecord_id(judgment.evidenceRef),
            contentDigest=judgment.evidenceSha256,
        )
        for judgment in record.judgments
    )
    return tuple(sorted(subrecords, key=lambda item: (item.subrecordId, item.contentDigest)))


def _judgment_subrecord_id(evidence_ref: str) -> str:
    """One deterministic subrecord identity for one exact judgment evidence reference."""

    return f"{_JUDGMENT_SUBRECORD_PREFIX}-{hashlib.sha256(evidence_ref.encode()).hexdigest()}"


def assemble_gate_five_inputs(
    *,
    memory_tree: str,
    affected_closure_plan_digest: str,
    checker_registry_digest: str,
    coherence_subrecords: Sequence[CoherenceSubrecordIdentity],
    candidate_pair_authority_digest: str,
) -> GateFiveSemanticInputs:
    """Assemble the exact R21 Gate-5 semantic inputs or refuse with a typed refusal."""

    subrecords = tuple(coherence_subrecords)
    if not subrecords:
        _refuse(
            "gate-five-coherence-subrecords-missing",
            "the Gate-5 certificate requires at least one canonical coherence subrecord",
            next_action="curator_coherence",
        )
    try:
        return GateFiveSemanticInputs(
            memoryTree=CandidateIdentity(kind="git-tree", value=memory_tree),
            affectedClosurePlanDigest=affected_closure_plan_digest,
            memoryCheckerRegistryDigest=checker_registry_digest,
            coherenceSubrecords=subrecords,
            candidatePairAuthorityDigest=candidate_pair_authority_digest,
        )
    except ValidationError as error:
        _refuse(
            "gate-five-inputs-invalid",
            f"the assembled Gate-5 semantic inputs failed closed validation: {error}",
            next_action="memory_quality_check",
        )


def _refuse(status: str, detail: str, *, next_action: str) -> NoReturn:
    raise FinalCertificationError(status, detail, next_action=next_action)


__all__ = ["assemble_gate_five_inputs", "coherence_subrecords"]
