"""CCR-L28 Gate-5 memory-domain R11 rail population contracts."""

from __future__ import annotations

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import RailDefinition
from agents_remember.memory_quality.final_certification.catalog import (
    FINAL_FULL_CATALOG_VERSION,
    complete_final_catalog,
)
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.memory_quality.incremental_scope.registry import (
    checker_registry_version,
)

_PROFILE_ID = "closeout-targeted"


def test_gate_five_rails_mirror_the_complete_final_catalog() -> None:
    rails = gate_five_memory_rails(_PROFILE_ID)
    catalog = complete_final_catalog()
    assert len(rails) == len(catalog) == 12
    assert tuple(rail.identity.railId for rail in rails) == tuple(item.itemId for item in catalog)
    for rail, catalog_item in zip(rails, catalog, strict=True):
        del catalog_item
        assert isinstance(rail, RailDefinition)
        assert rail.gate == 5
        assert rail.authority == "memory-domain"
        assert rail.railClass == "memory-quality"
        assert rail.applicability[0].profileId == _PROFILE_ID
        assert rail.applicability[0].status == "applicable"


def test_gate_five_rails_configuration_digest_is_deterministic_and_bound() -> None:
    first = gate_five_memory_rails(_PROFILE_ID)
    second = gate_five_memory_rails(_PROFILE_ID)
    assert first == second
    digests = {rail.adapter.configurationDigest for rail in first}
    assert len(digests) == 1
    expected = content_digest(
        {
            "catalogVersion": FINAL_FULL_CATALOG_VERSION,
            "checkerRegistry": checker_registry_version(),
            "population": [item.model_dump(mode="json") for item in complete_final_catalog()],
        }
    )
    assert digests == {expected}


def test_gate_five_rails_are_canonically_ordered_unique() -> None:
    rails = gate_five_memory_rails(_PROFILE_ID)
    keys = tuple(rail.identity.key for rail in rails)
    assert len(keys) == len(set(keys))
    assert keys == tuple(sorted(keys))
    assert tuple(rail.orderKey for rail in rails) == tuple(sorted(rail.orderKey for rail in rails))
