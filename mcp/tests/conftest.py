"""Local, isolated pytest; delivery certification is an explicit Dagger-only option."""

from __future__ import annotations

import os
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(REPOSITORY_ROOT / "mcp" / "src"),
    str(REPOSITORY_ROOT / "mcp" / "test_support"),
]

from agents_remember_test_support.testing.hermetic_bootstrap import (
    activate_current_pytest_environment,
    candidate_test_process,
)

# This is an actual pytest process, using the existing test isolation owner. No daemon,
# lifecycle-worker identity, or Dagger capability is supplied to the ordinary test loop.
_ENVIRONMENT_LEASE = activate_current_pytest_environment(
    candidate_test_process(REPOSITORY_ROOT), os.environ
)
_TEMPORARY = tempfile.TemporaryDirectory(prefix="ar-pytest-", dir="/tmp")
_ISOLATED_ROOT = Path(_TEMPORARY.name)
_ISOLATED_ENVIRONMENT = mock.patch.dict(
    os.environ,
    {
        "HOME": str(_ISOLATED_ROOT / "home"),
        "XDG_CONFIG_HOME": str(_ISOLATED_ROOT / "config"),
        "XDG_DATA_HOME": str(_ISOLATED_ROOT / "data"),
        "XDG_CACHE_HOME": str(_ISOLATED_ROOT / "cache"),
        "CODEX_HOME": str(_ISOLATED_ROOT / "codex"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    },
)
_ISOLATED_ENVIRONMENT.start()
for directory in ("home", "config", "data", "cache", "codex"):
    (_ISOLATED_ROOT / directory).mkdir()
# Inherited live opt-ins and credentials must never make an ordinary run contact a service.
# Tests that exercise these inputs construct their own local environment explicitly.
for name in tuple(os.environ):
    if (
        name.startswith(("AR_RUN_", "AR_SPAWN_", "AR_HOSTED_"))
        or name.endswith(("_API_KEY", "_ACCESS_TOKEN", "_AUTH_TOKEN"))
        or name
        in {
            "AGENTS_REMEMBER_REAL_MCP_CONFIG",
            "AR_CLAUDE_STREAM_SMOKE",
            "AR_CODEX_APP_SERVER_LIVE_SMOKE",
            "AR_CODEX_APP_SERVER_LIVE_CONFORMANCE",
            "SSH_AUTH_SOCK",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "AWS_PROFILE",
        }
    ):
        os.environ.pop(name, None)

pytest_plugins = ("agents_remember_test_support.testing.pytest_bootstrap",)
_INTEGRATION_FILES = pytest.StashKey[frozenset[Path]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--certify",
        action="store_true",
        help="load delivery evidence services after genuine Dagger admission",
    )


def pytest_configure(config: pytest.Config) -> None:
    # Read existing file membership once; no source census, dependency graph, or collection probe.
    with (REPOSITORY_ROOT / "mcp/tests/test-evidence-lanes.toml").open("rb") as stream:
        files = tomllib.load(stream)["files"]
    config.stash[_INTEGRATION_FILES] = frozenset(
        REPOSITORY_ROOT / path
        for category in ("integration", "stress-durability")
        for path in files[category]
    )
    if config.getoption("certify"):
        from agents_remember_test_support.testing.certifying_bootstrap import (  # noqa: PLC0415
            prepare_certifying_pytest_bootstrap,
        )
        from agents_remember_test_support.testing.dagger_admission import (  # noqa: PLC0415
            DaggerAdmissionError,
        )

        try:
            prepare_certifying_pytest_bootstrap(REPOSITORY_ROOT)
        except DaggerAdmissionError as error:
            raise pytest.UsageError(str(error)) from error
        config.pluginmanager.import_plugin(
            "agents_remember_test_support.pytest_certifying_bootstrap"
        )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    # Pure unit invocations do not import application/transaction test modules at all.
    if (
        config.option.markexpr == "not integration"
        and collection_path in config.stash[_INTEGRATION_FILES]
    ):
        return True
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.path in config.stash[_INTEGRATION_FILES]:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _integration_composition(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("integration") is not None and not request.config.getoption(
        "certify"
    ):
        request.getfixturevalue("worktree_services")
    yield


@pytest.fixture
def worktree_services() -> Iterator[None]:
    """Explicit application composition for boundary tests, never an autouse unit fixture."""
    from agents_remember.application.worktree_services import (  # noqa: PLC0415
        bind_worktree_services,
        build_default_worktree_services,
    )
    from agents_remember.worktrees.services import reset_worktree_services  # noqa: PLC0415

    bind_worktree_services(build_default_worktree_services())
    try:
        yield
    finally:
        reset_worktree_services()


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    _ISOLATED_ENVIRONMENT.stop()
    _ENVIRONMENT_LEASE.close()
    _TEMPORARY.cleanup()
