"""R11 Gate-5 memory-domain rail population (CCR-L28 bridge, R08 vocabulary).

The R21 registry contribution for one certifying admission must include the
Gate-5 rails the terminal Gate-5 phase actually executes.  The R08 complete
final catalog is the exhaustion surface of that phase, so this module derives
one R11 RailDefinition per final-catalog item.  The population is
deterministic:  the catalog identities and the memory checker registries are
source-stable, and every rail carries the same configuration digest over those
identities, so two derivations yield byte-identical registry contributions.
"""

from __future__ import annotations

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    RailAdapterDefinition,
    RailApplicability,
    RailDefinition,
    RailEvidenceContract,
    RailRuntimeInputs,
)
from agents_remember.memory_quality.final_certification.catalog import (
    FINAL_FULL_CATALOG_VERSION,
    complete_final_catalog,
)
from agents_remember.memory_quality.incremental_scope.registry import (
    checker_registry_version,
)
from agents_remember.models.certification.base import RailIdentity

_CORRECTIVE_OWNER = "memory-curator"
_ADAPTER_ID = "memory-final-certification"
_EVIDENCE_MAX_BYTES = 16 * 1024 * 1024


def gate_five_memory_rails(
    profile_id: str,
    *,
    configuration_digest: str | None = None,
) -> tuple[RailDefinition, ...]:
    """Return one applicable memory-domain R11 rail per complete final-catalog item.

    profile_id is the admitted selection identity (the R11 registry profile
    the admission freezes).  configuration_digest defaults to the canonical
    digest over the catalog version, the memory checker registry version, and
    the exact catalog population; passing it explicitly lets a caller bind an
    already-published run generation to the same rail identities.
    """
    catalog = complete_final_catalog()
    resolved_digest = configuration_digest or _catalog_configuration_digest()
    order_keys = {item.itemId: index for index, item in enumerate(catalog)}
    rails = tuple(
        RailDefinition(
            identity=RailIdentity(railId=item.itemId, version=item.version),
            gate=5,
            railClass="memory-quality",
            authority="memory-domain",
            ownerClass=_CORRECTIVE_OWNER,
            correctiveOwner=_CORRECTIVE_OWNER,
            posture="enforcing",
            orderKey=f"{order_keys[item.itemId]:04d}-{item.itemId}",
            prerequisites=(),
            requiredArtifacts=(),
            adapter=RailAdapterDefinition(
                adapterKind="memory-checker",
                adapterId=_ADAPTER_ID,
                configurationDigest=resolved_digest,
                executionEvidence=f"memory-checker://{item.itemId}",
            ),
            runtimeInputs=RailRuntimeInputs(runtimeIdentity="memory-quality-gate-five"),
            applicability=(
                RailApplicability(
                    profileId=profile_id,
                    status="applicable",
                    selectionIdentity="memory-final-full-catalog",
                    population="complete final memory/coherence certification population",
                ),
            ),
            evidenceContract=(
                RailEvidenceContract(
                    evidenceId=item.itemId,
                    mediaType="application/json",
                    maxBytes=_EVIDENCE_MAX_BYTES,
                ),
            ),
            outputArtifacts=(),
        )
        for item in catalog
    )
    return tuple(sorted(rails, key=lambda rail: rail.orderKey))


def _catalog_configuration_digest() -> str:
    population = complete_final_catalog()
    return content_digest(
        {
            "catalogVersion": FINAL_FULL_CATALOG_VERSION,
            "checkerRegistry": checker_registry_version(),
            "population": [item.model_dump(mode="json") for item in population],
        }
    )


__all__ = ["gate_five_memory_rails"]
