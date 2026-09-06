"""Consumer metadata selection uses both sides without launching pytest recursively."""

from pathlib import Path

import pytest
from agents_remember_test_support.code_quality.catalog_selection import changed_catalog_consumers
from agents_remember_test_support.code_quality.dependency_ownership import GLOBAL_TEST_INPUTS


def _catalog(consumers: str, *, scope: str = "exact") -> str:
    return f'''schema_version = "ar-test-evidence-lifecycle/v3"
[[artifact]]
path = "mcp/tests/helper.py"
consumer_scope = "{scope}"
consumers = [{consumers}]
'''


def test_removed_and_added_consumers_both_remain_affected() -> None:
    assert changed_catalog_consumers(
        _catalog('"mcp/tests/test_old.py"'), _catalog('"mcp/tests/test_new.py"')
    ) == frozenset({Path("mcp/tests/test_old.py"), Path("mcp/tests/test_new.py")})


def test_comment_and_consumer_order_changes_do_not_select_tests() -> None:
    assert (
        changed_catalog_consumers(
            _catalog('"a.py", "b.py"'), "# comment\n" + _catalog('"b.py", "a.py"')
        )
        == frozenset()
    )


@pytest.mark.parametrize(
    "after",
    [
        _catalog('"a.py"', scope="exact-source"),
        _catalog('"a.py"').replace("helper.py", "other.py"),
        _catalog('"a.py"').replace("v3", "v4"),
        _catalog('"a.py"') + '\n[[artifact]]\npath = "new.py"\n',
    ],
)
def test_non_consumer_policy_changes_keep_global_selection(after: str) -> None:
    assert changed_catalog_consumers(_catalog('"a.py"'), after) is None


def test_population_configuration_is_global_but_consumer_metadata_is_not() -> None:
    assert Path("pyproject.toml") in GLOBAL_TEST_INPUTS
    assert Path("mcp/tests/test-evidence-lanes.toml") in GLOBAL_TEST_INPUTS
    assert Path("mcp/tests/evidence-lifecycle.toml") not in GLOBAL_TEST_INPUTS
