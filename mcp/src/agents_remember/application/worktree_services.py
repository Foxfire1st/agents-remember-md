"""Default WorktreeServices composition for the MCP/CLI entry points."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from agents_remember.application import provider_runtime as provider_runtime_api
from agents_remember.memory_quality import check as memory_quality_check_api
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.memory_quality.incremental_scope.candidate import observe_contract_task
from agents_remember.memory_quality.style.citations import (
    source_index_cache as citation_cache_api,
)
from agents_remember.models.task_document import CanonicalTaskObservation
from agents_remember.providers import provider_setup as provider_setup_api
from agents_remember.worktrees.services import (
    ProviderSetupRequestSpec,
    TerminalGuard,
    WorktreeServices,
    bind_worktree_services,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


class ProviderLifecycleAdapter:
    """Providers-backed implementation of :class:`ProviderLifecyclePort`."""

    def load_settings(self, settings_path: Path) -> dict[str, Any] | None:
        return provider_setup_api.load_settings(settings_path)

    def provider_enabled(self, settings: dict[str, Any], provider_id: str) -> bool:
        return provider_setup_api.provider_enabled(settings, provider_id)

    def settings_path(self, settings_path: Path) -> Path:
        return provider_setup_api.settings_path(settings_path)

    def setup_request(self, *, spec: ProviderSetupRequestSpec) -> Any:
        return provider_setup_api.ProviderSetupRequest(
            action=spec.action,
            coordination_root=spec.coordination_root,
            settings_path=spec.settings_path,
            timeout=spec.timeout,
            dry_run=spec.dry_run,
            skip_grepai=spec.skip_grepai,
            cgc_seed=spec.cgc_seed,
            cgc_isolated=spec.cgc_isolated,
            grepai_seed=spec.grepai_seed,
            grepai_isolated=spec.grepai_isolated,
        )

    def cgc_seed_options(
        self,
        *,
        source_coordination_root: Path,
        repo_id: str,
        source_repo_root: Path,
        target_repo_root: Path,
    ) -> Any:
        return provider_setup_api.CgcSeedOptions(
            source_coordination_root=source_coordination_root,
            repo_id=repo_id,
            source_repo_root=source_repo_root,
            target_repo_root=target_repo_root,
        )

    def isolated_cgc_options(self, *, runtime_root: Path) -> Any:
        return provider_setup_api.IsolatedCgcOptions(runtime_root=runtime_root)

    def grepai_seed_options(
        self,
        *,
        source_coordination_root: Path,
        source_settings_path: Path,
        project_id: str,
        target_memory_root: Path | None,
    ) -> Any:
        return provider_setup_api.GrepaiSeedOptions(
            source_coordination_root=source_coordination_root,
            source_settings_path=source_settings_path,
            project_id=project_id,
            target_memory_root=target_memory_root,
        )

    def isolated_grepai_options(
        self,
        *,
        runtime_root: Path,
        project_id: str,
        target_memory_root: Path | None,
        allow_missing_roots: bool,
    ) -> Any:
        return provider_setup_api.IsolatedGrepaiOptions(
            runtime_root=runtime_root,
            project_id=project_id,
            target_memory_root=target_memory_root,
            allow_missing_roots=allow_missing_roots,
        )

    def run_setup(self, request: Any) -> dict[str, Any]:
        return provider_setup_api.run_provider_setup(request)

    def launch_setup(
        self,
        *,
        request: Any,
        contract: WorktreeContract,
        write_state_file: Any,
        settings_cleanup: Path | None,
    ) -> dict[str, Any]:
        return provider_runtime_api.launch_provider_setup(
            provider_runtime_api.ProviderSetupJob(
                request=request,
                contract=contract,
                write_state_file=write_state_file,
                settings_cleanup=settings_cleanup,
            )
        )

    def setup_running(self, contract: WorktreeContract) -> bool:
        return provider_runtime_api.provider_setup_running(contract)

    def setup_status(self, contract: WorktreeContract) -> dict[str, Any] | None:
        return provider_runtime_api.provider_setup_status(contract)

    def teardown(self, contract: WorktreeContract, *, dry_run: bool) -> dict[str, Any]:
        return provider_runtime_api.teardown_worktree_providers(contract, dry_run=dry_run)

    def remove_tree(self, path: Path, *, dry_run: bool) -> dict[str, Any]:
        return provider_runtime_api.remove_tree(path, dry_run=dry_run)


class CertificationMemoryRailsAdapter:
    """memory_quality-backed implementation of :class:."""

    def memory_rails(self, profile_id: str):
        return gate_five_memory_rails(profile_id)


class MemoryQualityAdapter:
    """memory_quality-backed implementation of :class:`MemoryQualityPort`."""

    def observe_contract_task(self, contract: WorktreeContract) -> CanonicalTaskObservation:
        return observe_contract_task(contract)

    def check_groups(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (
            tuple(memory_quality_check_api.BEFORE_METADATA_REFRESH_CHECKS),
            tuple(memory_quality_check_api.AFTER_METADATA_REFRESH_CHECKS),
        )

    def drift_context(
        self,
        code_repository_root: Path,
        context: Any,
        detail_limit: int,
        *,
        unstamped_code_commit: str | None = None,
    ) -> Any:
        return memory_quality_check_api.DriftCheckContext(
            code_repository_root=code_repository_root,
            context=context,
            detail_limit=detail_limit,
            unstamped_code_commit=unstamped_code_commit,
        )

    def run_check(
        self,
        onboarding_root: Path,
        *,
        checks: tuple[str, ...] | None,
        drift_context: Any,
    ) -> dict[str, Any]:
        return memory_quality_check_api.run_memory_quality_check(
            onboarding_root,
            checks=list(checks) if checks is not None else None,
            drift_context=drift_context,
        )


class CitationGuardAdapter:
    """memory_quality-backed implementation of :class:`CitationGuardPort`."""

    def guard(
        self,
        contract: WorktreeContract,
        *,
        requested_contract_path: Path,
    ) -> AbstractContextManager[TerminalGuard]:
        return citation_cache_api.terminal_namespace_guard(
            contract,
            requested_contract_path=requested_contract_path,
        )


def build_default_worktree_services() -> WorktreeServices:
    return WorktreeServices(
        provider_lifecycle=ProviderLifecycleAdapter(),
        memory_quality=MemoryQualityAdapter(),
        citation_guard=CitationGuardAdapter(),
        certification_memory_rails=CertificationMemoryRailsAdapter(),
    )


__all__ = [
    "CertificationMemoryRailsAdapter",
    "CitationGuardAdapter",
    "MemoryQualityAdapter",
    "ProviderLifecycleAdapter",
    "bind_worktree_services",
    "build_default_worktree_services",
]
