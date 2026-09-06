"""Service ports the worktree lifecycle needs from packages above it.

Worktrees ranks below providers, memory_quality and code_quality, so the
lifecycle modules never import them. The composition layer binds one
``WorktreeServices`` bundle (provider lifecycle, memory-quality gate,
citation-cache guard) before invoking worktree operations.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from agents_remember.certification.models import RailDefinition
from agents_remember.models.task_document import CanonicalTaskObservation
from agents_remember.worktrees.worktree_contract import WorktreeContract

if TYPE_CHECKING:
    from agents_remember.certification.certificate_models import GateFiveSemanticInputs
    from agents_remember.worktrees.integration.closeout.certification.execution import (
        CloseoutCertificationHandoff,
    )
    from agents_remember.worktrees.integration.closeout.preparation.memory_port import (
        PreparedMemoryCertificationPort,
    )
    from agents_remember.worktrees.modules.models import WorktreeCommandResult


class TerminalGuard(Protocol):
    """The citation-cache terminal fence surface worktrees consume."""

    def preview(self) -> dict[str, object]: ...
    def complete(
        self,
        *,
        outcome: Literal["completed", "abandoned"],
        publish: Callable[[], None],
        rollback_publish: Callable[[], None],
    ) -> Any: ...


class CitationGuardPort(Protocol):
    def guard(
        self,
        contract: WorktreeContract,
        *,
        requested_contract_path: Path,
    ) -> AbstractContextManager[TerminalGuard]: ...


class ProviderLifecyclePort(Protocol):
    def load_settings(self, settings_path: Path) -> dict[str, Any] | None: ...
    def provider_enabled(self, settings: dict[str, Any], provider_id: str) -> bool: ...
    def settings_path(self, settings_path: Path) -> Path: ...
    def setup_request(self, *, spec: ProviderSetupRequestSpec) -> Any: ...
    def cgc_seed_options(
        self,
        *,
        source_coordination_root: Path,
        repo_id: str,
        source_repo_root: Path,
        target_repo_root: Path,
    ) -> Any: ...
    def isolated_cgc_options(self, *, runtime_root: Path) -> Any: ...
    def grepai_seed_options(
        self,
        *,
        source_coordination_root: Path,
        source_settings_path: Path,
        project_id: str,
        target_memory_root: Path | None,
    ) -> Any: ...
    def isolated_grepai_options(
        self,
        *,
        runtime_root: Path,
        project_id: str,
        target_memory_root: Path | None,
        allow_missing_roots: bool,
    ) -> Any: ...
    def run_setup(self, request: Any) -> dict[str, Any]: ...
    def launch_setup(
        self,
        *,
        request: Any,
        contract: WorktreeContract,
        write_state_file: Callable[[dict[str, Any]], Path],
        settings_cleanup: Path | None,
    ) -> dict[str, Any]: ...
    def setup_running(self, contract: WorktreeContract) -> bool: ...
    def setup_status(self, contract: WorktreeContract) -> dict[str, Any] | None: ...
    def teardown(self, contract: WorktreeContract, *, dry_run: bool) -> dict[str, Any]: ...
    def remove_tree(self, path: Path, *, dry_run: bool) -> dict[str, Any]: ...


class CertificationMemoryRailsPort(Protocol):
    """Bound provider of the R11 Gate-5 memory-domain rail population.

    The worktree layer may not import memory_quality; the composition layer
    (application/MCP/CLI) binds an adapter that derives the memory rails from
    the memory checker registries.  profile_id is the admitted selection
    identity the R11 registry profile freezes.
    """

    def memory_rails(self, profile_id: str) -> Sequence[RailDefinition]: ...


class MemoryQualityPort(Protocol):
    def observe_contract_task(self, contract: WorktreeContract) -> CanonicalTaskObservation: ...
    def check_groups(self) -> tuple[tuple[str, ...], tuple[str, ...]]: ...
    def drift_context(
        self,
        code_repository_root: Path,
        context: Any,
        detail_limit: int,
        *,
        unstamped_code_commit: str | None = None,
    ) -> Any: ...
    def run_check(
        self,
        onboarding_root: Path,
        *,
        checks: tuple[str, ...] | None,
        drift_context: Any,
    ) -> dict[str, Any]: ...


class CertificationContinuationPort(Protocol):
    """Composition-owned Gate 5 and finalization boundaries after exact code certificates."""

    def observe_memory(
        self, handoff: CloseoutCertificationHandoff
    ) -> GateFiveSemanticInputs | None:
        """Read and verify current memory authority; absence cannot authorize Gate-5 reuse."""
        ...

    def run_memory(self, handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult: ...
    def finalize(self, handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult: ...


@dataclass(frozen=True)
class WorktreeServices:
    provider_lifecycle: ProviderLifecyclePort
    memory_quality: MemoryQualityPort
    citation_guard: CitationGuardPort
    certification_memory_rails: CertificationMemoryRailsPort | None = None
    certification_continuation: CertificationContinuationPort | None = None
    prepared_memory_certification: PreparedMemoryCertificationPort | None = None


@dataclass(frozen=True)
class ProviderSetupRequestSpec:
    """Everything one provider setup request needs, as the worktree layer sees it.

    The provider-owned option payloads (cgc/grepai seed and isolation options)
    are opaque to worktrees: the port builds them from the primitive fields.
    """

    action: str
    coordination_root: Path
    settings_path: Path
    timeout: int
    dry_run: bool
    skip_grepai: bool
    cgc_seed: Any
    cgc_isolated: Any
    grepai_seed: Any
    grepai_isolated: Any


_current: WorktreeServices | None = None


class WorktreeServicesUnboundError(RuntimeError):
    """Raised when a worktree operation needs services that were never bound."""


def bind_worktree_services(services: WorktreeServices) -> None:
    """Bind the composition-provided services for this process."""
    global _current  # noqa: PLW0603  # module-level composition binding
    _current = services


def reset_worktree_services() -> None:
    """Clear the bound services (tests and process teardown)."""
    global _current  # noqa: PLW0603  # module-level composition binding
    _current = None


def worktree_services() -> WorktreeServices:
    if _current is None:
        raise WorktreeServicesUnboundError(
            "worktree services are not bound; the MCP/CLI composition must bind them"
        )
    return _current


__all__ = [
    "CertificationContinuationPort",
    "CertificationMemoryRailsPort",
    "CitationGuardPort",
    "MemoryQualityPort",
    "ProviderLifecyclePort",
    "TerminalGuard",
    "WorktreeServices",
    "WorktreeServicesUnboundError",
    "bind_worktree_services",
    "reset_worktree_services",
    "worktree_services",
]
