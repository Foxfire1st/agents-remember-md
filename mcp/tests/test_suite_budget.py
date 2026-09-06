"""Collection budgets count parametrized items without spawning another collection."""

from types import SimpleNamespace
from typing import cast

import conftest
import pytest


class _Item:
    def __init__(self, integration: bool) -> None:
        self.integration = integration

    def get_closest_marker(self, name: str) -> bool | None:
        assert name == "integration"
        return True if self.integration else None


@pytest.mark.parametrize(
    ("units", "integrations", "exceeded"),
    [(1000, 150, None), (1001, 0, "unit"), (0, 151, "integration")],
)
def test_selected_case_budgets(units: int, integrations: int, exceeded: str | None) -> None:
    config = SimpleNamespace(
        getini={"unit_case_budget": 1000, "integration_case_budget": 150}.__getitem__
    )
    session = cast(
        pytest.Session,
        SimpleNamespace(items=[_Item(False)] * units + [_Item(True)] * integrations, config=config),
    )
    if exceeded:
        with pytest.raises(pytest.UsageError, match=rf"{exceeded} suite.*explicit"):
            conftest.pytest_collection_finish(session)
    else:
        conftest.pytest_collection_finish(session)
