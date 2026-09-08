"""Task-derived ownership for integration refs that are never ordinary workbenches."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import MasterExecutionNature, TaskDocument
from agents_remember.tasks.document_refs import (
    ResolvedTaskDocument,
    TaskDocumentRefError,
    TaskDocumentTopology,
    repository_master_documents,
)
from agents_remember.worktrees.atomic_series_seal import require_series_accepting_leaves
from agents_remember.worktrees.integration.integration_branch_repository import (
    branch_worktree_owners,
    canonical_local_branch,
    memory_repository_default_branch,
    repository_default_branch,
)
from agents_remember.worktrees.integration.integration_branch_types import (
    IntegrationSurface,
    IntegrationSurfaceKind,
    IntegrationSurfaceSide,
    IntegrationTarget,
    IntegrationTargetKind,
    ProposedWorkBranches,
    RepositoryCheckoutRequest,
    _BranchScope,
    _MasterAuthority,
    _RepositorySide,
)
from agents_remember.worktrees.integration.integration_topology_collisions import (
    TopologyCollisionRequest,
    TopologyCollisionServices,
    require_no_live_leaf_collisions,
)
from agents_remember.worktrees.integration.integration_topology_repair import (
    current_surfaces_for_publication,
)
from agents_remember.worktrees.modules.git import (
    branch_exists,
    current_branch,
    repository_identity,
)
from agents_remember.worktrees.scheduling_mode import (
    TERMINAL_SERIES_CLEANUP,
    commanded_sprint_masters,
    effective_execution_nature,
)
from agents_remember.worktrees.source_lineage import require_current_source_lineage
from agents_remember.worktrees.task_resolver import (
    series_contract_path,
    slugify,
)
from agents_remember.worktrees.worktree_contract import (
    ContractError,
    WorktreeContract,
    load_contract,
)


def integration_surfaces(contract: WorktreeContract) -> tuple[IntegrationSurface, ...]:
    return _integration_surfaces(_scope(contract))


def integration_targets(contract: WorktreeContract) -> tuple[IntegrationTarget, ...]:
    sides = _repository_sides(contract)
    authority = _master_authority(
        _BranchScope(
            coordination_root=contract.coordination_root,
            repo_name=contract.repo_name,
            task_root=contract.task_root,
            sides=sides,
        )
    )
    default_by_side = {side.side: _side_default_branch(side) for side in sides}
    if contract.kind == "leaf":
        kind, branch, owner = _leaf_target(contract, authority)
    elif contract.kind == "series":
        if authority.sprint_ref is None or authority.sprint_branch is None:
            default = default_by_side["code"]
            if (
                canonical_local_branch(contract.code_repo_path, contract.code_source_branch)
                != default
            ):
                raise RuntimeError(
                    "standalone atomic series source must be the configured repository-default "
                    "branch; only the PR landing plane may move that root"
                )
            raise RuntimeError(
                "generic integration refuses a standalone atomic series -> repository-default "
                "landing; publish it through the PR landing plane"
            )
        else:
            _require_atomic_master(authority)
            branch = authority.sprint_branch
            kind = "sprint-super"
            owner = _ref_key(authority.sprint_ref)
    else:
        raise RuntimeError(
            f"integration target requires a leaf or series contract, got {contract.kind!r}"
        )

    targets: list[IntegrationTarget] = []
    for side in sides:
        source = canonical_local_branch(side.repository, side.source_branch)
        expected = canonical_local_branch(side.repository, branch)
        default = default_by_side[side.side]
        if source != expected:
            raise RuntimeError(
                f"{side.side} integration source {source!r} does not match task-derived "
                f"target {expected!r}"
            )
        if expected == default:
            raise RuntimeError(
                f"generic integration refuses repository-default {side.side} branch "
                f"{expected!r}; the repository landing plane must move the PR-gated root"
            )
        surface = _surface(side, kind=kind, branch=expected, owner=owner)
        targets.append(
            IntegrationTarget(
                side=surface.side,
                kind=kind,
                repository=surface.repository,
                branch=surface.branch,
                owner=surface.owner,
            )
        )
    return tuple(targets)


def require_proposed_work_branches(proposal: ProposedWorkBranches) -> None:
    """Refuse protected work-branch requests before start can mutate Git."""

    sides = [
        _RepositorySide(
            side="code",
            repository=proposal.code_repository,
            worktree=proposal.code_repository,
            source_branch="",
            work_branch=proposal.code_work_branch,
        )
    ]
    if proposal.memory_repository is not None:
        if _repository_identity(proposal.memory_repository, "memory") == _repository_identity(
            proposal.code_repository, "code"
        ):
            raise RuntimeError("external memory must not share the code repository Git common-dir")
        sides.append(
            _RepositorySide(
                side="memory",
                repository=proposal.memory_repository,
                worktree=proposal.memory_repository,
                source_branch="",
                work_branch=proposal.memory_work_branch,
            )
        )
    scope = _BranchScope(
        proposal.coordination_root,
        proposal.repo_name,
        proposal.task_root,
        tuple(sides),
    )
    surfaces = _integration_surfaces(scope)
    for side in scope.sides:
        _require_unprotected_branch(side, surfaces, operation="worktree_start")


def require_ordinary_worktree(contract: WorktreeContract, *, operation: str) -> None:
    """Refuse a leaf whose proposed or checked-out work branch is protected."""

    if contract.kind != "leaf":
        return
    surfaces = integration_surfaces(contract)
    for side in _repository_sides(contract):
        _require_unprotected_branch(side, surfaces, operation=operation)
        if not side.worktree.exists():
            continue
        expected_identity = _repository_identity(side.repository, side.side)
        actual_identity = repository_identity(side.worktree)
        if actual_identity != expected_identity:
            raise RuntimeError(
                f"{operation} refused: {side.side} worktree does not belong to its recorded "
                "repository"
            )
        checked_out = canonical_local_branch(side.repository, current_branch(side.worktree))
        expected = canonical_local_branch(side.repository, side.work_branch)
        if checked_out != expected:
            raise RuntimeError(
                f"{operation} refused: {side.side} worktree has {checked_out!r} checked out, "
                f"expected {expected!r}"
            )
        _require_unprotected_name(side, checked_out, surfaces, operation=operation)


def require_terminal_worktree(contract: WorktreeContract, *, operation: str) -> None:
    """Refuse terminal deletion when a contract names another owner's protected ref."""

    if contract.kind == "leaf":
        require_ordinary_worktree(contract, operation=operation)
        return
    if contract.kind != "series":
        raise RuntimeError(f"{operation} refused: unsupported contract kind {contract.kind!r}")
    authority = require_series_contract_authority(contract, operation=operation)
    if operation == "worktree_cleanup":
        master = authority.topology.resolve(authority.master_ref)
        if master.document.status != "Completed":
            raise RuntimeError(
                "worktree_cleanup refused: atomic master task must be Completed before its "
                "integration branch can retire"
            )
    expected = f"ar/{slugify(contract.task_root.name)}"
    for side in _repository_sides(contract):
        spelled = side.work_branch.strip().removeprefix("refs/heads/")
        if spelled != expected:
            raise RuntimeError(
                f"{operation} refused: series {side.side} branch must use exact task-owned "
                f"spelling {expected!r}; aliases cannot be retired"
            )
        actual = canonical_local_branch(side.repository, side.work_branch)
        if actual != canonical_local_branch(side.repository, expected):
            raise RuntimeError(
                f"{operation} refused: series {side.side} branch {actual!r} is not its "
                f"task-owned atomic branch {expected!r}"
            )
        default = _side_default_branch(side)
        parent = (
            canonical_local_branch(side.repository, authority.sprint_branch)
            if authority.sprint_branch is not None
            else None
        )
        if actual in (default, parent):
            raise RuntimeError(f"{operation} refused: protected parent ref cannot be retired")


def require_series_contract_authority(
    contract: WorktreeContract,
    *,
    operation: str,
) -> _MasterAuthority:
    """Bind an atomic series contract to its exact task-derived repositories and refs."""

    if contract.kind != "series":
        raise RuntimeError(f"{operation} requires an atomic series contract")
    scope = _scope(contract)
    authority = _master_authority(scope)
    _require_atomic_master(authority)
    _require_series_identity(
        scope,
        contract,
        contract.task_root,
        authority.sprint_branch,
    )
    if authority.sprint_branch is None:
        for side in _repository_sides(contract):
            source = canonical_local_branch(side.repository, side.source_branch)
            default = _side_default_branch(side)
            if source != default:
                raise RuntimeError(
                    f"{operation} refused: standalone atomic {side.side} source must be "
                    f"repository-default {default!r}, got {source!r}"
                )
    return authority


def require_parent_series_accepting_leaves(
    contract: WorktreeContract,
    *,
    operation: str,
) -> WorktreeContract | None:
    """Return an atomic leaf's open parent, or None for organizational direct-super work."""

    authority = _master_authority(_scope(contract))
    if authority.sprint_ref is not None and authority.execution_nature == "organizational":
        return None
    _require_atomic_master(authority)
    parent_path = contract.parent_contract_path or series_contract_path(contract.task_root)
    if not parent_path.is_file():
        raise RuntimeError(f"{operation} requires its exact parent series contract")
    series = _load_series(parent_path)
    _require_series_identity(
        _scope(contract),
        series,
        contract.task_root,
        authority.sprint_branch,
    )
    require_series_accepting_leaves(series, operation=operation)
    return series


def atomic_leaf_parent(contract: WorktreeContract, *, operation: str) -> WorktreeContract | None:
    """Resolve the exact open atomic owner before deferring leaf-wide acceptance."""
    if contract.kind != "leaf":
        return None
    topology = TaskDocumentTopology(contract.coordination_root)
    owner = topology.canonical_ref(contract.repo_name, contract.task_root / "task.json")
    if topology.resolve(owner).document.kind != "master":
        return None
    require_ordinary_worktree(contract, operation=operation)
    parent = require_parent_series_accepting_leaves(contract, operation=operation)
    if parent is not None:
        require_current_source_lineage(contract, operation=operation)
    return parent


def require_sync_worktree(contract: WorktreeContract) -> None:
    """Bind sync to an ordinary leaf or the exact task-owned atomic series refs."""

    if contract.kind == "leaf":
        require_ordinary_worktree(contract, operation="worktree_sync")
        return
    if contract.kind == "series":
        require_series_contract_authority(contract, operation="worktree_sync")
        return
    raise RuntimeError(f"worktree_sync refused: unsupported contract kind {contract.kind!r}")


def require_source_branch_write(
    contract: WorktreeContract, *, side_name: IntegrationSurfaceSide, operation: str
) -> None:
    """Refuse ordinary start/recovery code that would write a protected source ref."""

    sides = _repository_sides(contract)
    side = next(item for item in sides if item.side == side_name)
    surfaces = integration_surfaces(contract)
    source = canonical_local_branch(side.repository, side.source_branch)
    for surface in surfaces:
        if (
            surface.side == side.side
            and surface.repository == _repository_identity(side.repository, side.side)
            and surface.branch == source
        ):
            raise RuntimeError(
                f"{operation} refused: protected {side.side} source {source!r} moves only "
                "through its task-owned landing plane"
            )


def require_ordinary_repository_checkout(request: RepositoryCheckoutRequest) -> None:
    """Refuse a repo-level writer when its selected checkout owns a protected ref.

    Carryover is intentionally contract-independent at planning time. Its apply path still
    needs the same repository-global branch census as task work, so it supplies the configured
    repositories and the exact checkout it proposes to mutate here.
    """

    sides = [
        _RepositorySide(
            "code",
            request.code_repository,
            request.code_repository,
            "",
            "",
        )
    ]
    if request.memory_repository is not None:
        if _repository_identity(request.code_repository, "code") == _repository_identity(
            request.memory_repository, "memory"
        ):
            raise RuntimeError(
                f"{request.operation} refused: external memory must not share the code "
                "repository Git common-dir"
            )
        sides.append(
            _RepositorySide(
                "memory",
                request.memory_repository,
                request.checkout if request.side_name == "memory" else request.memory_repository,
                "",
                "",
            )
        )
    selected = next((side for side in sides if side.side == request.side_name), None)
    if selected is None:
        raise RuntimeError(
            f"{request.operation} cannot resolve the configured {request.side_name} repository"
        )
    expected_identity = _repository_identity(selected.repository, selected.side)
    if repository_identity(request.checkout) != expected_identity:
        raise RuntimeError(
            f"{request.operation} refused: selected {request.side_name} checkout belongs to "
            "another repository"
        )
    scope = _BranchScope(
        request.coordination_root,
        request.repo_name,
        request.coordination_root / "tasks" / request.repo_name,
        tuple(sides),
    )
    branch = canonical_local_branch(selected.repository, current_branch(request.checkout))
    _require_unprotected_name(
        selected,
        branch,
        _integration_surfaces(scope),
        operation=request.operation,
    )


def require_topology_publication_authority(
    coordination_root: Path,
    repo_name: str,
    code_repository: Path,
    memory_repository: Path | None,
    overrides: Mapping[TaskDocumentRef, TaskDocument],
) -> None:
    """Refuse a task edit that would convert a live leaf workbench into authority."""

    scope = _publication_scope(
        coordination_root,
        repo_name,
        code_repository,
        memory_repository,
    )
    candidate = _integration_surfaces(scope, overrides=overrides)
    current, repaired_owners = current_surfaces_for_publication(
        candidate,
        coordination_root,
        overrides,
        lambda: _integration_surfaces(scope),
    )
    _require_stable_surface_owners(current, candidate)
    require_no_live_leaf_collisions(
        TopologyCollisionRequest(
            scope=scope,
            current=current,
            candidate=candidate,
            overrides=overrides,
            repaired_owners=repaired_owners,
        ),
        _topology_collision_services(),
    )
    _require_new_surface_availability(scope, current, candidate, allow_existing_super=False)


def require_topology_migration_authority(
    coordination_root: Path,
    repo_name: str,
    code_repository: Path,
    memory_repository: Path | None,
    overrides: Mapping[TaskDocumentRef, TaskDocument],
) -> None:
    """Validate the explicit legacy cutover without inventing pre-migration authority."""

    scope = _publication_scope(
        coordination_root,
        repo_name,
        code_repository,
        memory_repository,
    )
    candidate = _integration_surfaces(scope, overrides=overrides)
    require_no_live_leaf_collisions(
        TopologyCollisionRequest(
            scope=scope,
            current=(),
            candidate=candidate,
            overrides=overrides,
        ),
        _topology_collision_services(),
    )
    _require_new_surface_availability(scope, (), candidate, allow_existing_super=True)


def _publication_scope(
    coordination_root: Path,
    repo_name: str,
    code_repository: Path,
    memory_repository: Path | None,
) -> _BranchScope:
    sides = [_RepositorySide("code", code_repository, code_repository, "", "")]
    if memory_repository is not None:
        code_identity = _repository_identity(code_repository, "code")
        memory_identity = repository_identity(memory_repository)
        if memory_identity is not None and memory_identity != code_identity:
            sides.append(_RepositorySide("memory", memory_repository, memory_repository, "", ""))
    return _BranchScope(
        coordination_root,
        repo_name,
        coordination_root / "tasks" / repo_name,
        tuple(sides),
    )


def _require_new_surface_availability(
    scope: _BranchScope,
    current: tuple[IntegrationSurface, ...],
    candidate: tuple[IntegrationSurface, ...],
    *,
    allow_existing_super: bool,
) -> None:
    current_keys = {_surface_key(surface) for surface in current}
    for surface in candidate:
        if not _surface_requires_availability(surface, current_keys, allow_existing_super):
            continue
        _require_atomic_surface_available(scope, surface)
        _require_surface_not_checked_out(scope, surface)


def _surface_requires_availability(
    surface: IntegrationSurface,
    current_keys: set[tuple[str, Path, str]],
    allow_existing_super: bool,
) -> bool:
    if surface.kind == "repository-default" or _surface_key(surface) in current_keys:
        return False
    return not (surface.kind == "sprint-super" and allow_existing_super)


def _require_atomic_surface_available(
    scope: _BranchScope,
    surface: IntegrationSurface,
) -> None:
    collision = all(
        (
            surface.kind == "atomic-integration",
            branch_exists(surface.repository, surface.branch),
            not _atomic_surface_has_series(scope, surface),
        )
    )
    if collision:
        raise RuntimeError(
            "task topology publication refused: proposed atomic branch already exists "
            f"without its exact task-owned series contract: {surface.branch!r}"
        )


def _require_surface_not_checked_out(
    scope: _BranchScope,
    surface: IntegrationSurface,
) -> None:
    owners = branch_worktree_owners(surface.repository, surface.branch)
    collision = (bool(owners), _atomic_surface_has_series(scope, surface)) == (True, False)
    if collision:
        raise RuntimeError(
            "task topology publication refused: proposed protected "
            f"{surface.side} branch {surface.branch!r} is already checked out at "
            f"{[path.as_posix() for path in owners]!r}"
        )


def _atomic_surface_has_series(scope: _BranchScope, surface: IntegrationSurface) -> bool:
    if surface.kind != "atomic-integration":
        return False
    prefix = f"{scope.repo_name}/"
    if not surface.owner.startswith(prefix):
        return False
    master_path = (
        scope.coordination_root / "tasks" / scope.repo_name / surface.owner.removeprefix(prefix)
    )
    path = series_contract_path(master_path.parent)
    if not path.is_file():
        return False
    series = _load_series(path)
    side = next(item for item in _repository_sides(series) if item.side == surface.side)
    return _branch_key(side, side.work_branch) == _surface_key(surface)


def _scope(contract: WorktreeContract) -> _BranchScope:
    return _BranchScope(
        coordination_root=contract.coordination_root,
        repo_name=contract.repo_name,
        task_root=contract.task_root,
        sides=_repository_sides(contract),
    )


def _integration_surfaces(
    scope: _BranchScope,
    *,
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None = None,
) -> tuple[IntegrationSurface, ...]:
    surfaces = [
        _surface(
            side,
            kind="repository-default",
            branch=_side_default_branch(side),
            owner="repository default branch",
        )
        for side in scope.sides
    ]
    topology = TaskDocumentTopology(scope.coordination_root)
    try:
        masters = _repository_masters_with_overrides(topology, scope.repo_name, overrides)
    except TaskDocumentRefError as exc:
        raise RuntimeError(
            f"cannot resolve integration-branch authority: {exc.status}: {exc}"
        ) from exc
    commanded_refs: set[TaskDocumentRef] = set()
    for sprint in masters:
        if not sprint.document.orchestrates:
            continue
        branch = sprint.document.integrationBranch
        if not branch:
            raise RuntimeError(
                f"integration-branch authority requires {sprint.ref.key} to declare "
                "integrationBranch"
            )
        try:
            commanded = commanded_sprint_masters(topology, sprint, overrides=overrides)
        except TaskDocumentRefError as exc:
            raise RuntimeError(
                f"cannot resolve integration-branch authority: {exc.status}: {exc}"
            ) from exc
        commanded_refs.update(master.ref for master in commanded)
        surfaces.extend(
            _surface(
                side,
                kind="sprint-super",
                branch=branch,
                owner=sprint.ref.key,
            )
            for side in scope.sides
        )
        for master in commanded:
            surfaces.extend(
                _master_integration_surfaces(
                    scope,
                    master,
                    branch,
                    nature=effective_execution_nature(master.document, sprint.document),
                )
            )
    for master in masters:
        if (
            master.ref in commanded_refs
            or master.document.orchestrates
            or effective_execution_nature(master.document, None) != "atomic"
        ):
            continue
        surfaces.extend(
            _master_integration_surfaces(
                scope,
                master,
                None,
                nature=effective_execution_nature(master.document, None),
            )
        )
    return _deduplicated(surfaces)


def _repository_masters_with_overrides(
    topology: TaskDocumentTopology,
    repo_name: str,
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None,
) -> tuple[ResolvedTaskDocument, ...]:
    masters = {master.ref: master for master in repository_master_documents(topology, repo_name)}
    root = topology.coordination_root / "tasks" / repo_name
    for ref, document in (overrides or {}).items():
        _apply_master_override(masters, root, repo_name, ref, document)
    ordered = sorted(masters, key=lambda item: item.key)
    return tuple(map(masters.__getitem__, ordered))


def _apply_master_override(
    masters: dict[TaskDocumentRef, ResolvedTaskDocument],
    root: Path,
    repo_name: str,
    ref: TaskDocumentRef,
    document: TaskDocument,
) -> None:
    if ref.repository != repo_name:
        return
    if document.kind != "master":
        masters.pop(ref, None)
        return
    masters[ref] = ResolvedTaskDocument(ref, root / ref.path, document)


def _require_stable_surface_owners(
    current: tuple[IntegrationSurface, ...],
    candidate: tuple[IntegrationSurface, ...],
) -> None:
    current_by_key = {_surface_key(surface): surface for surface in current}
    for surface in candidate:
        existing = current_by_key.get(_surface_key(surface))
        if existing is not None and (
            existing.kind != surface.kind or existing.owner != surface.owner
        ):
            raise RuntimeError(
                "task topology publication refused: protected branch ownership would change "
                f"from {existing.kind} owned by {existing.owner} to {surface.kind} owned by "
                f"{surface.owner}"
            )


def _topology_collision_services() -> TopologyCollisionServices:
    return TopologyCollisionServices(
        repository_masters=_repository_masters_with_overrides,
        repository_sides=_repository_sides,
        load_contract=_load_series,
        branch_key=_branch_key,
    )


def _branch_key(side: _RepositorySide, branch: str) -> tuple[str, Path, str]:
    return (
        side.side,
        _repository_identity(side.repository, side.side),
        canonical_local_branch(side.repository, branch),
    )


def _surface_key(surface: IntegrationSurface) -> tuple[str, Path, str]:
    return surface.side, surface.repository, surface.branch


def _master_integration_surfaces(
    scope: _BranchScope,
    master: ResolvedTaskDocument,
    super_branch: str | None,
    *,
    nature: MasterExecutionNature,
) -> list[IntegrationSurface]:
    path = series_contract_path(master.path.parent)
    expected = f"ar/{slugify(master.path.parent.name)}"
    if nature == "organizational":
        if path.is_file() and _load_series(path).cleanup not in TERMINAL_SERIES_CLEANUP:
            raise RuntimeError(
                f"organizational master {master.ref.key} retains a live series contract; "
                "retire it with worktree_cleanup or worktree_abandon before ordinary work starts"
            )
        return []
    if not path.is_file():
        return [
            _surface(
                side,
                kind="atomic-integration",
                branch=expected,
                owner=master.ref.key,
            )
            for side in scope.sides
        ]
    series = _load_series(path)
    _require_series_identity(scope, series, master.path.parent, super_branch)
    # A completed atomic task keeps owning its canonical branch name until archival.
    # Cleanup may delete the ref, but it must not make the name available as a workbench.
    return _series_surfaces(scope.sides, series, owner=master.ref.key)


def _master_authority(scope: _BranchScope) -> _MasterAuthority:
    topology = TaskDocumentTopology(scope.coordination_root)
    try:
        master_ref = topology.canonical_ref(scope.repo_name, scope.task_root / "task.json")
        master = topology.resolve(master_ref)
        sprint_ref = topology.parent(master_ref)
    except TaskDocumentRefError as exc:
        raise RuntimeError(
            f"cannot resolve integration-branch authority: {exc.status}: {exc}"
        ) from exc
    if sprint_ref is None:
        return _MasterAuthority(
            topology,
            master_ref,
            None,
            None,
            effective_execution_nature(master.document, None),
        )
    try:
        sprint = topology.resolve(sprint_ref)
        if sprint.document.executionGraph is not None:
            topology.validate_execution_topology(sprint_ref)
    except TaskDocumentRefError as exc:
        raise RuntimeError(
            f"cannot resolve integration-branch authority: {exc.status}: {exc}"
        ) from exc
    branch = sprint.document.integrationBranch
    if not branch:
        raise RuntimeError(
            f"integration-branch authority requires {sprint.ref.key} to declare integrationBranch"
        )
    return _MasterAuthority(
        topology,
        master_ref,
        sprint_ref,
        branch,
        effective_execution_nature(master.document, sprint.document),
    )


def _leaf_target(
    contract: WorktreeContract, authority: _MasterAuthority
) -> tuple[IntegrationTargetKind, str, str]:
    if authority.sprint_ref is not None:
        if authority.execution_nature == "organizational":
            assert authority.sprint_branch is not None
            return "sprint-super", authority.sprint_branch, _ref_key(authority.sprint_ref)
        _require_atomic_master(authority)
    series = require_parent_series_accepting_leaves(
        contract,
        operation="atomic leaf integration",
    )
    assert series is not None
    return "atomic-integration", series.code_work_branch, contract.task_root.name


def _require_atomic_master(authority: _MasterAuthority) -> None:
    if authority.execution_nature != "atomic":
        raise RuntimeError(
            f"series integration requires executionNature='atomic', got "
            f"{authority.execution_nature!r}"
        )


def _repository_sides(contract: WorktreeContract) -> tuple[_RepositorySide, ...]:
    sides = [
        _RepositorySide(
            side="code",
            repository=contract.code_repo_path,
            worktree=contract.code_worktree,
            source_branch=contract.code_source_branch,
            work_branch=contract.code_work_branch,
        )
    ]
    if contract.memory_mode == "external":
        if contract.memory_repo_path is None:
            raise RuntimeError("external-memory branch authority requires a repository path")
        if _repository_identity(contract.memory_repo_path, "memory") == _repository_identity(
            contract.code_repo_path, "code"
        ):
            raise RuntimeError("external memory must not share the code repository Git common-dir")
        memory_worktree = (
            contract.memory_repo_path if contract.kind == "series" else contract.memory_worktree
        )
        if memory_worktree is None:
            raise RuntimeError("external-memory leaf branch authority requires a worktree path")
        sides.append(
            _RepositorySide(
                side="memory",
                repository=contract.memory_repo_path,
                worktree=memory_worktree,
                source_branch=contract.memory_source_branch,
                work_branch=contract.memory_work_branch,
            )
        )
    return tuple(sides)


def _load_series(path: Path) -> WorktreeContract:
    try:
        return load_contract(path)
    except (ContractError, OSError) as exc:
        raise RuntimeError(
            f"cannot resolve integration-branch authority from {path}: {exc}"
        ) from exc


def _series_surfaces(
    current_sides_input: tuple[_RepositorySide, ...],
    series: WorktreeContract,
    *,
    owner: str,
) -> list[IntegrationSurface]:
    current_sides = {side.side: side for side in current_sides_input}
    series_sides = {side.side: side for side in _repository_sides(series)}
    if set(series_sides) != set(current_sides):
        raise RuntimeError(f"atomic series {owner} does not match the repository memory edge")
    surfaces: list[IntegrationSurface] = []
    for name, current_side in current_sides.items():
        series_side = series_sides[name]
        current_identity = _repository_identity(current_side.repository, name)
        series_identity = _repository_identity(series_side.repository, name)
        if series_identity != current_identity:
            raise RuntimeError(
                f"atomic series {owner} points its {name} branch at a different repository"
            )
        surfaces.append(
            _surface(
                series_side,
                kind="atomic-integration",
                branch=series_side.work_branch,
                owner=owner,
            )
        )
    return surfaces


def _require_series_identity(
    scope: _BranchScope,
    series: WorktreeContract,
    task_root: Path,
    super_branch: str | None,
) -> None:
    expected_root = task_root.resolve()
    expected_work_branch = f"ar/{slugify(task_root.name)}"
    _require_series_contract_shape(scope, series, expected_root, expected_work_branch)
    _require_series_code_source(series, expected_root, super_branch)
    _series_surfaces(scope.sides, series, owner=expected_root.as_posix())
    _require_series_memory_identity(
        series,
        expected_root,
        expected_work_branch,
        super_branch,
    )


def _require_series_contract_shape(
    scope: _BranchScope,
    series: WorktreeContract,
    expected_root: Path,
    expected_work_branch: str,
) -> None:
    if (
        series.kind != "series"
        or series.repo_name != scope.repo_name
        or series.task_root.resolve() != expected_root
        or series.contract_path.resolve() != series_contract_path(expected_root).resolve()
        or canonical_local_branch(series.code_repo_path, series.code_work_branch)
        != canonical_local_branch(series.code_repo_path, expected_work_branch)
    ):
        raise RuntimeError(
            f"series integration-branch authority does not match commanded task {expected_root}"
        )


def _require_series_code_source(
    series: WorktreeContract,
    expected_root: Path,
    super_branch: str | None,
) -> None:
    if super_branch is not None and canonical_local_branch(
        series.code_repo_path, series.code_source_branch
    ) != canonical_local_branch(series.code_repo_path, super_branch):
        raise RuntimeError(
            f"series code source does not match the sprint super for {expected_root}"
        )
    if super_branch is None and canonical_local_branch(
        series.code_repo_path, series.code_source_branch
    ) != repository_default_branch(series.code_repo_path):
        raise RuntimeError(
            f"standalone atomic series code source is not the repository default for "
            f"{expected_root}"
        )


def _require_series_memory_identity(
    series: WorktreeContract,
    expected_root: Path,
    expected_work_branch: str,
    super_branch: str | None,
) -> None:
    if series.memory_mode != "external":
        return
    assert series.memory_repo_path is not None
    if canonical_local_branch(
        series.memory_repo_path, series.memory_work_branch
    ) != canonical_local_branch(series.memory_repo_path, expected_work_branch):
        raise RuntimeError(f"series memory target does not match commanded task {expected_root}")
    if super_branch is not None and canonical_local_branch(
        series.memory_repo_path, series.memory_source_branch
    ) != canonical_local_branch(series.memory_repo_path, super_branch):
        raise RuntimeError(
            f"series memory source does not match the sprint super for {expected_root}"
        )
    if super_branch is None and canonical_local_branch(
        series.memory_repo_path, series.memory_source_branch
    ) != memory_repository_default_branch(series.memory_repo_path):
        raise RuntimeError(
            f"standalone atomic series memory source is not the repository default for "
            f"{expected_root}"
        )


def _side_default_branch(side: _RepositorySide) -> str:
    if side.side == "memory":
        return memory_repository_default_branch(side.repository)
    return repository_default_branch(side.repository)


def _surface(
    side: _RepositorySide,
    *,
    kind: IntegrationSurfaceKind,
    branch: str,
    owner: str,
) -> IntegrationSurface:
    return IntegrationSurface(
        side=side.side,
        kind=kind,
        repository=_repository_identity(side.repository, side.side),
        branch=canonical_local_branch(side.repository, branch),
        owner=owner,
    )


def _repository_identity(repository: Path, side: str) -> Path:
    identity = repository_identity(repository)
    if identity is None:
        raise RuntimeError(f"cannot resolve {side} integration repository identity: {repository}")
    return identity


def _require_unprotected_branch(
    side: _RepositorySide,
    surfaces: tuple[IntegrationSurface, ...],
    *,
    operation: str,
) -> None:
    branch = canonical_local_branch(side.repository, side.work_branch)
    _require_unprotected_name(side, branch, surfaces, operation=operation)


def _require_unprotected_name(
    side: _RepositorySide,
    branch: str,
    surfaces: tuple[IntegrationSurface, ...],
    *,
    operation: str,
) -> None:
    identity = _repository_identity(side.repository, side.side)
    for surface in surfaces:
        if (
            surface.side == side.side
            and surface.repository == identity
            and surface.branch == branch
        ):
            raise RuntimeError(
                f"{operation} refused: integration-branch-is-not-a-workbench: "
                f"{side.side} branch {branch!r} is {surface.kind} owned by {surface.owner}"
            )


def _deduplicated(surfaces: list[IntegrationSurface]) -> tuple[IntegrationSurface, ...]:
    unique: dict[tuple[str, Path, str], IntegrationSurface] = {}
    for surface in surfaces:
        key = (surface.side, surface.repository, surface.branch)
        existing = unique.get(key)
        if existing is not None and (
            existing.kind != surface.kind or existing.owner != surface.owner
        ):
            raise RuntimeError(
                f"integration-branch authority collision for {surface.side} "
                f"{surface.branch!r}: {existing.kind} owned by {existing.owner} conflicts "
                f"with {surface.kind} owned by {surface.owner}"
            )
        unique.setdefault(key, surface)
    return tuple(unique.values())


def _ref_key(value: TaskDocumentRef) -> str:
    return value.key
