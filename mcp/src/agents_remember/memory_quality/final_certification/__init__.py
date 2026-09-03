"""Final full memory-coherence certification (CCR-R08 Gate 5).

The complete validation surface that certifies full memory coherence over one exact
code/memory candidate pair: the deterministic complete final catalog (plan and
content-addressed subresults), the Gate 1-4 prerequisite adapter, the coherence
record and candidate-pair authority binding, and the R21 Gate-5 semantic-input
assembly. The certification itself never mutates code or memory.
"""

from agents_remember.memory_quality.final_certification.catalog import (
    FINAL_FULL_CATALOG_VERSION,
    compile_final_catalog_plan,
    complete_final_catalog,
    final_catalog_attestation,
    final_catalog_readiness,
)
from agents_remember.memory_quality.final_certification.certificate import (
    assemble_gate_five_inputs,
    coherence_subrecords,
)
from agents_remember.memory_quality.final_certification.certify import (
    certify_final_full_memory_coherence,
)
from agents_remember.memory_quality.final_certification.gate_prefix import (
    GateFourPrefixProof,
    require_green_gate_prefix,
)
from agents_remember.memory_quality.final_certification.models import (
    FinalCatalogItemIdentity,
    FinalCatalogItemResult,
    FinalCertificationResult,
    FinalFullCatalogAttestation,
    FinalFullCatalogPlan,
)

__all__ = [
    "FINAL_FULL_CATALOG_VERSION",
    "FinalCatalogItemIdentity",
    "FinalCatalogItemResult",
    "FinalCertificationResult",
    "FinalFullCatalogAttestation",
    "FinalFullCatalogPlan",
    "GateFourPrefixProof",
    "assemble_gate_five_inputs",
    "certify_final_full_memory_coherence",
    "coherence_subrecords",
    "compile_final_catalog_plan",
    "complete_final_catalog",
    "final_catalog_attestation",
    "final_catalog_readiness",
    "require_green_gate_prefix",
]
