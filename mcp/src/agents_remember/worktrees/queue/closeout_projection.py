"""Exact canonical source census for disposable closeout projections."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from agents_remember.controlplane.closeout_queue_store import ProjectionSourceIdentity
from agents_remember.models.closeout.projection import (
    MAX_CLOSEOUT_CANDIDATES,
    MAX_CLOSEOUT_SOURCE_PROBLEMS,
    CloseoutProjectionMember,
    ProjectionSourceClassification,
    ProjectionSourceProblem,
)
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import SprintExecutionGraph, TaskDocument
from agents_remember.tasks.document_refs import (
    ResolvedTaskDocument,
    TaskDocumentRefError,
    TaskDocumentTopology,
)
from agents_remember.worktrees.integration.closeout.door_evidence import (
    capture_door_candidate_evidence,
    door_candidate_evidence_blockers,
)
from agents_remember.worktrees.scheduling_mode import (
    TERMINAL_SERIES_CLEANUP,
    commanded_sprint_masters,
    effective_execution_nature,
)
from agents_remember.worktrees.task_resolver import series_contract_path
from agents_remember.worktrees.worktree_contract import (
    ContractError,
    WorktreeContract,
    load_contract,
)

from .closeout_projection_activation import project_series_activation
from .closeout_projection_members import (
    ProjectionMemberContext,
    projection_member,
    scheduling_source_fact,
)
from .closeout_projection_snapshot import ProjectionSourceSnapshot
from .closeout_projection_source_facts import (
    TaskSourceProjectionError,
    semantic_topology_source_fact,
    task_source_fact,
)
from .closeout_queue_errors import CloseoutQueueError
from .closeout_queue_evidence import GradeAuthority, planning_authorities
from .closeout_queue_graph import QueueGraphContext, graph_context

_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_CLASSIFICATION_RANK = {"ready": 0, "waiting": 1, "blocked": 2}


@dataclass(frozen=True)
class _DoorSource:
    address: Path
    contract: WorktreeContract | None
    door: CloseoutDoorGeneration | None


_ProjectionDoor = tuple[
    _DoorSource,
    ResolvedTaskDocument,
    ResolvedTaskDocument,
    int,
]


@dataclass(frozen=True)
class _TaskCensus:
    topology: TaskDocumentTopology
    sprint: ResolvedTaskDocument
    mode: Literal["dag", "atomic-sequential"]
    masters: tuple[ResolvedTaskDocument, ...]
    task_rows: tuple[dict[str, object], ...]
    leafs: tuple[tuple[ResolvedTaskDocument, ResolvedTaskDocument, int], ...]


@dataclass(frozen=True)
class _DoorCensus:
    series_rows: tuple[dict[str, object], ...]
    door_rows: tuple[dict[str, object], ...]
    activation_waiting: Mapping[TaskDocumentRef, tuple[str, ...]]
    waiting: tuple[_ProjectionDoor, ...]


@dataclass(frozen=True)
class _TaskCensusBuild:
    task_rows: list[dict[str, object]]
    problems: list[ProjectionSourceProblem]
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None


@dataclass(frozen=True)
class _SeriesProjectionContext:
    tasks: _TaskCensus
    problems: list[ProjectionSourceProblem]
    door_rows: list[dict[str, object]]
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None


@dataclass(frozen=True)
class _SeriesSourceObservation:
    row: dict[str, object]
    live_contract: WorktreeContract | None = None
    door: tuple[_DoorSource, ResolvedTaskDocument, int] | None = None


class _ProjectionSourceRefusal(RuntimeError):
    def __init__(self, problem: ProjectionSourceProblem) -> None:
        self.problem = problem
        super().__init__(problem.errorType)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def capture_projection_source(
    coordination_root: Path,
    sprint_ref: TaskDocumentRef,
    *,
    timestamp: str | None = None,
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None = None,
) -> ProjectionSourceSnapshot:
    """Census current task/enclosure/series sources at exact canonical addresses."""

    captured_at = timestamp or now_iso()
    problems: list[ProjectionSourceProblem] = []
    try:
        tasks = _task_census(coordination_root, sprint_ref, problems, overrides)
    except _ProjectionSourceRefusal as exc:
        return _unreadable_snapshot((exc.problem,), captured_at)
    except TaskSourceProjectionError as exc:
        problem = _problem(
            "task",
            exc.address,
            "task-document-field-effect-unclassified",
            "classify every task-document field before rebuilding",
        )
        return _unreadable_snapshot((problem,), captured_at)
    doors = _door_census(coordination_root, tasks, problems, overrides)
    _require_waiting_door_identities(doors.waiting, sprint_ref, problems)

    payload = {
        "schema": "closeout-projection-source/v1",
        "sprint": sprint_ref.model_dump(mode="json"),
        "mode": tasks.mode,
        "tasks": list(tasks.task_rows),
        "series": list(doors.series_rows),
        "doors": list(doors.door_rows),
    }
    if problems:
        return _unreadable_snapshot(tuple(_bounded_problems(problems, sprint_ref)), captured_at)
    classification: ProjectionSourceClassification = (
        "terminal" if tasks.sprint.document.status == "Completed" else "active"
    )
    if classification == "terminal":
        return ProjectionSourceSnapshot(
            ProjectionSourceIdentity(
                _fingerprint(payload),
                classification=classification,
            ),
            classification,
            (),
            captured_at,
        )

    try:
        try:
            graph = (
                graph_context(
                    tasks.topology,
                    sprint_ref,
                    authored_graph=cast(
                        SprintExecutionGraph,
                        tasks.sprint.document.executionGraph,
                    ),
                    strict_registers=False,
                    overrides=overrides,
                )
                if tasks.mode == "dag"
                else None
            )
            grade_authority = (
                graph.grade_authority
                if graph is not None
                else GradeAuthority(tasks.sprint, *planning_authorities(tasks.sprint))
            )
        except CloseoutQueueError as exc:
            raise _ProjectionSourceRefusal(
                _problem(
                    "task",
                    sprint_ref.key,
                    exc.status,
                    "repair the canonical planning registers before rebuilding",
                )
            ) from exc
        members, member_source_facts = _projection_members(
            tasks,
            doors,
            graph=graph,
            grade_authority=grade_authority,
        )
    except _ProjectionSourceRefusal as exc:
        return _unreadable_snapshot((exc.problem,), captured_at)
    payload["memberSources"] = member_source_facts
    payload["members"] = [member.model_dump(mode="json") for member in members]
    return ProjectionSourceSnapshot(
        ProjectionSourceIdentity(
            _fingerprint(payload),
            classification=classification,
            members=tuple(members),
        ),
        classification,
        tuple(members),
        captured_at,
    )


def _unreadable_snapshot(
    problems: tuple[ProjectionSourceProblem, ...],
    captured_at: str,
) -> ProjectionSourceSnapshot:
    return ProjectionSourceSnapshot(ProjectionSourceIdentity(None, problems), None, (), captured_at)


def _task_census(
    coordination_root: Path,
    sprint_ref: TaskDocumentRef,
    problems: list[ProjectionSourceProblem],
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None,
) -> _TaskCensus:
    topology = TaskDocumentTopology(coordination_root)
    try:
        sprint = topology.resolve(sprint_ref, overrides)
        masters = commanded_sprint_masters(topology, sprint, overrides=overrides)
    except TaskDocumentRefError as exc:
        raise _ProjectionSourceRefusal(
            _problem("task", sprint_ref.key, exc.status, "repair canonical task topology")
        ) from exc
    if sprint.document.kind != "master" or not sprint.document.orchestrates:
        raise _ProjectionSourceRefusal(
            _problem(
                "task",
                sprint_ref.key,
                "sprint-required",
                "address an orchestration sprint task document",
            )
        )
    task_rows: list[dict[str, object]] = [task_source_fact(sprint)]
    leafs: list[tuple[ResolvedTaskDocument, ResolvedTaskDocument, int]] = []
    build = _TaskCensusBuild(task_rows, problems, overrides)
    for master_index, master in enumerate(masters):
        task_rows.append(task_source_fact(master))
        leafs.extend(
            _master_leafs(
                topology,
                master,
                master_index,
                build,
            )
        )
    mode: Literal["dag", "atomic-sequential"] = (
        "dag" if sprint.document.executionGraph is not None else "atomic-sequential"
    )
    return _TaskCensus(topology, sprint, mode, masters, tuple(task_rows), tuple(leafs))


def _door_census(
    coordination_root: Path,
    tasks: _TaskCensus,
    problems: list[ProjectionSourceProblem],
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None,
) -> _DoorCensus:
    series_rows, activation_waiting, series_doors = _series_sources(
        tasks.sprint,
        tasks.masters,
        problems,
    )
    door_rows: list[dict[str, object]] = []
    doors = _leaf_projection_doors(coordination_root, tasks, problems, door_rows)
    doors.extend(_series_projection_doors(tasks, series_doors, problems, door_rows, overrides))
    waiting = tuple(
        row for row in doors if row[0].door is not None and row[0].door.disposition == "waiting"
    )
    return _DoorCensus(tuple(series_rows), tuple(door_rows), activation_waiting, waiting)


def _leaf_projection_doors(
    coordination_root: Path,
    tasks: _TaskCensus,
    problems: list[ProjectionSourceProblem],
    door_rows: list[dict[str, object]],
) -> list[_ProjectionDoor]:
    doors: list[_ProjectionDoor] = []
    for leaf, master, order in tasks.leafs:
        for source in _leaf_door_sources(coordination_root, leaf, problems):
            door_rows.append(_door_fact(source))
            if source.contract is not None and source.contract.kind != "leaf":
                problems.append(
                    _problem(
                        "door",
                        source.address.as_posix(),
                        "enclosure-contract-kind-invalid",
                        "repair the exact leaf enclosure contract",
                    )
                )
                continue
            if source.door is None:
                continue
            if not _leaf_door_identity_matches(source, leaf, master, tasks.sprint):
                problems.append(
                    _problem(
                        "door",
                        source.address.as_posix(),
                        "door-canonical-identity-mismatch",
                        "publish a successor from the exact task-owned enclosure",
                    )
                )
                continue
            doors.append((source, leaf, master, order))
    return doors


def _leaf_door_identity_matches(
    source: _DoorSource,
    leaf: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    sprint: ResolvedTaskDocument,
) -> bool:
    door = source.door
    return bool(
        door is not None
        and door.taskDocumentRef == leaf.ref
        and door.owningMasterTaskDocumentRef == master.ref
        and door.sprintTaskDocumentRef == sprint.ref
        and door.contractPath == source.address.as_posix()
    )


def _series_projection_doors(
    tasks: _TaskCensus,
    series_doors: list[tuple[_DoorSource, ResolvedTaskDocument, int]],
    problems: list[ProjectionSourceProblem],
    door_rows: list[dict[str, object]],
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None,
) -> list[_ProjectionDoor]:
    doors: list[_ProjectionDoor] = []
    context = _SeriesProjectionContext(tasks, problems, door_rows, overrides)
    for source, master, order in series_doors:
        resolved = _series_projection_door(context, source, master, order)
        if resolved is not None:
            doors.append(resolved)
    return doors


def _series_projection_door(
    context: _SeriesProjectionContext,
    source: _DoorSource,
    master: ResolvedTaskDocument,
    order: int,
) -> _ProjectionDoor | None:
    context.door_rows.append(_door_fact(source))
    door = cast(CloseoutDoorGeneration, source.door)
    if not _series_door_identity_matches(source, master, context.tasks.sprint):
        context.problems.append(
            _problem(
                "door",
                source.address.as_posix(),
                "door-canonical-identity-mismatch",
                "publish a successor from the exact series contract owner",
            )
        )
        return None
    try:
        candidate = _resolved_series_candidate(
            context.tasks,
            door,
            master,
            context.overrides,
        )
    except TaskDocumentRefError as exc:
        context.problems.append(
            _problem(
                "door",
                source.address.as_posix(),
                exc.status,
                "repair the series door candidate task reference",
            )
        )
        return None
    return source, candidate, master, order


def _resolved_series_candidate(
    tasks: _TaskCensus,
    door: CloseoutDoorGeneration,
    master: ResolvedTaskDocument,
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None,
) -> ResolvedTaskDocument:
    candidate = tasks.topology.resolve(door.taskDocumentRef, overrides)
    if tasks.topology.parent(candidate.ref) != master.ref:
        raise TaskDocumentRefError(
            "task-document-parent-mismatch",
            "series door candidate is not owned by its canonical master",
        )
    return candidate


def _series_door_identity_matches(
    source: _DoorSource,
    master: ResolvedTaskDocument,
    sprint: ResolvedTaskDocument,
) -> bool:
    door = source.door
    return bool(
        door is not None
        and door.owningMasterTaskDocumentRef == master.ref
        and door.sprintTaskDocumentRef == sprint.ref
        and door.contractPath == source.address.as_posix()
    )


def _require_waiting_door_identities(
    waiting: tuple[_ProjectionDoor, ...],
    sprint_ref: TaskDocumentRef,
    problems: list[ProjectionSourceProblem],
) -> None:
    generation_ids = [row[0].door.generationId for row in waiting if row[0].door is not None]
    task_refs = [row[1].ref.key for row in waiting]
    if len(waiting) > MAX_CLOSEOUT_CANDIDATES:
        problems.append(
            _problem(
                "door",
                sprint_ref.key,
                "waiting-door-cap-exceeded",
                "withdraw excess door generations before rebuilding",
            )
        )
    if len(generation_ids) != len(set(generation_ids)) or len(task_refs) != len(set(task_refs)):
        problems.append(
            _problem(
                "door",
                sprint_ref.key,
                "waiting-door-identity-conflict",
                "withdraw conflicting current door generations before rebuilding",
            )
        )


def _projection_members(
    tasks: _TaskCensus,
    doors: _DoorCensus,
    *,
    graph: QueueGraphContext | None,
    grade_authority: GradeAuthority,
) -> tuple[list[CloseoutProjectionMember], list[dict[str, object]]]:
    members: list[CloseoutProjectionMember] = []
    member_source_facts: list[dict[str, object]] = []
    topology_sprint = graph.sprint if graph is not None else tasks.sprint
    for source, leaf, master, order in doors.waiting:
        door = source.door
        contract = source.contract
        assert door is not None and contract is not None
        source_blockers: list[str] = []
        source_fact: dict[str, object]
        try:
            current_evidence = capture_door_candidate_evidence(contract, leaf)
        except CloseoutQueueError as exc:
            source_blockers.append(exc.status)
            source_fact = {
                "address": source.address.as_posix(),
                "state": "blocked",
                "errorType": exc.status,
            }
        else:
            source_blockers.extend(
                door_candidate_evidence_blockers(contract, door, current_evidence)
            )
            source_fact = {
                "address": source.address.as_posix(),
                "state": "current",
                **current_evidence.fingerprint_fact(contract),
            }
        scheduling_blockers, scheduling_fact = scheduling_source_fact(
            door,
            leaf,
            master,
            grade_authority,
        )
        source_blockers.extend(scheduling_blockers)
        source_fact["scheduling"] = scheduling_fact
        try:
            topology_fingerprint, topology_fact = semantic_topology_source_fact(
                topology_sprint,
                master,
                leaf,
                graph,
            )
        except CloseoutQueueError as exc:
            raise _ProjectionSourceRefusal(
                ProjectionSourceProblem(
                    kind="task",
                    address=leaf.ref.key,
                    state="invalid",
                    errorType=exc.status,
                    repairAction=exc.detail,
                )
            ) from exc
        source_fact["semanticTopology"] = topology_fact
        member_source_facts.append(source_fact)
        members.append(
            projection_member(
                ProjectionMemberContext(
                    source_address=source.address,
                    door=door,
                    candidate=leaf,
                    master=master,
                    order=order,
                    sprint=topology_sprint,
                    graph=graph,
                    task_topology_fingerprint=topology_fingerprint,
                    activation_waiting=doors.activation_waiting.get(master.ref, ()),
                    source_blockers=tuple(source_blockers),
                )
            )
        )
    members.sort(
        key=lambda member: (
            _CLASSIFICATION_RANK[member.classification],
            _PRIORITY_RANK[member.priority],
            member.order,
            member.taskDocumentRef.key,
        )
    )
    return members, member_source_facts


def _master_leafs(
    topology: TaskDocumentTopology,
    master: ResolvedTaskDocument,
    master_index: int,
    build: _TaskCensusBuild,
) -> list[tuple[ResolvedTaskDocument, ResolvedTaskDocument, int]]:
    leafs: list[tuple[ResolvedTaskDocument, ResolvedTaskDocument, int]] = []
    for leaf_index, row in enumerate(master.document.subTasks):
        if not row.file:
            continue
        path = (master.path.parent / row.file).with_suffix(".json")
        try:
            ref = topology.canonical_ref(master.ref.repository, path)
        except TaskDocumentRefError as exc:
            build.problems.append(
                _problem("task", path.as_posix(), exc.status, "repair the exact leaf task source")
            )
            continue
        if not path.is_file() and (build.overrides is None or ref not in build.overrides):
            build.task_rows.append({"address": path.as_posix(), "state": "missing"})
            build.problems.append(
                _problem(
                    "task",
                    path.as_posix(),
                    "task-document-missing",
                    "publish or detach the referenced leaf task source",
                )
            )
            continue
        try:
            leaf = topology.resolve(ref, build.overrides)
        except TaskDocumentRefError as exc:
            build.problems.append(
                _problem("task", path.as_posix(), exc.status, "repair the exact leaf task source")
            )
            continue
        build.task_rows.append(task_source_fact(leaf))
        leafs.append((leaf, master, master_index * 1000 + leaf_index))
    return leafs


def _series_sources(
    sprint: ResolvedTaskDocument,
    masters: tuple[ResolvedTaskDocument, ...],
    problems: list[ProjectionSourceProblem],
) -> tuple[
    list[dict[str, object]],
    Mapping[TaskDocumentRef, tuple[str, ...]],
    list[tuple[_DoorSource, ResolvedTaskDocument, int]],
]:
    rows: list[dict[str, object]] = []
    activation_waiting: dict[TaskDocumentRef, tuple[str, ...]] = {}
    doors: list[tuple[_DoorSource, ResolvedTaskDocument, int]] = []
    for order, master in enumerate(masters):
        observed = _series_source(sprint, master, order, problems)
        if observed.live_contract is not None:
            _observe_series_activation(
                observed,
                master,
                problems,
                activation_waiting,
            )
        rows.append(observed.row)
        if observed.door is not None:
            doors.append(observed.door)
    return rows, activation_waiting, doors


def _observe_series_activation(
    observed: _SeriesSourceObservation,
    master: ResolvedTaskDocument,
    problems: list[ProjectionSourceProblem],
    activation_waiting: dict[TaskDocumentRef, tuple[str, ...]],
) -> None:
    contract = observed.live_contract
    assert contract is not None
    activation = project_series_activation(contract, master.ref)
    observed.row["activation"] = activation.source_fact
    activation_waiting[master.ref] = activation.waiting
    if activation.problem is not None:
        problems.append(activation.problem)


def _series_source(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    order: int,
    problems: list[ProjectionSourceProblem],
) -> _SeriesSourceObservation:
    path = series_contract_path(master.path.parent)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return _SeriesSourceObservation({"address": path.as_posix(), "state": "missing"})
    except OSError as exc:
        return _unreadable_series_source(path, type(exc).__name__, problems)
    if not stat.S_ISREG(mode):
        return _unreadable_series_source(path, "series-contract-nonregular", problems)
    try:
        contract = load_contract(path)
    except (ContractError, OSError, UnicodeError, ValueError) as exc:
        return _unreadable_series_source(path, type(exc).__name__, problems)
    canonical = bool(
        contract.kind == "series"
        and contract.contract_path.resolve() == path.resolve()
        and contract.task_artifact.with_suffix(".json").resolve() == master.path.resolve()
    )
    nature = _series_nature(sprint, master, path, problems)
    live = (
        canonical
        and nature == "atomic"
        and contract.integration_status != "completed"
        and contract.cleanup not in TERMINAL_SERIES_CLEANUP
    )
    row = {
        "address": path.as_posix(),
        "state": "present",
        "kind": contract.kind,
        "cleanup": contract.cleanup,
        "integrationStatus": contract.integration_status,
        "canonical": canonical,
        "live": live,
    }
    if not canonical:
        problems.append(
            _problem(
                "series",
                path.as_posix(),
                "series-contract-authority-invalid",
                "repair or retire the exact series contract authority",
            )
        )
        return _SeriesSourceObservation(row)
    door = (
        (_DoorSource(path, contract, contract.closeout_door), master, order * 1000)
        if contract.closeout_door is not None
        else None
    )
    return _SeriesSourceObservation(row, contract if live else None, door)


def _unreadable_series_source(
    path: Path,
    error_type: str,
    problems: list[ProjectionSourceProblem],
) -> _SeriesSourceObservation:
    problems.append(
        _problem(
            "series",
            path.as_posix(),
            error_type,
            "repair or retire the exact series contract authority",
        )
    )
    return _SeriesSourceObservation({"address": path.as_posix(), "state": "unreadable"})


def _series_nature(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    path: Path,
    problems: list[ProjectionSourceProblem],
) -> str | None:
    try:
        return effective_execution_nature(master.document, sprint.document)
    except TaskDocumentRefError as exc:
        problems.append(
            _problem(
                "series",
                path.as_posix(),
                exc.status,
                "repair the exact master execution nature",
            )
        )
        return None


def _leaf_door_sources(
    coordination_root: Path,
    candidate: ResolvedTaskDocument,
    problems: list[ProjectionSourceProblem],
) -> list[_DoorSource]:
    sources: list[_DoorSource] = []
    for enclosure in candidate.document.enclosures:
        # Preserve the exact declared address until after lstat.  Resolving first would turn a
        # dangling symlink into target absence and would allow a live authority cell to redirect
        # the projection reader to another file.
        path = Path(enclosure.enclosurePath).absolute()
        canonical_root = coordination_root.resolve(strict=False)
        if not path.is_relative_to(canonical_root):
            problems.append(
                _problem(
                    "door",
                    path.as_posix(),
                    "outside-coordination-root",
                    "repair the task-owned enclosure address",
                )
            )
            continue
        try:
            canonical_parent = path.parent.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            problems.append(
                _problem(
                    "door",
                    path.as_posix(),
                    type(exc).__name__,
                    "repair the exact enclosure contract ancestry before closeout admission",
                )
            )
            sources.append(_DoorSource(path, None, None))
            continue
        if canonical_parent != path.parent:
            problems.append(
                _problem(
                    "door",
                    path.as_posix(),
                    "enclosure-contract-ancestor-noncanonical",
                    "replace symbolic-link ancestry with the exact canonical enclosure path",
                )
            )
            sources.append(_DoorSource(path, None, None))
            continue
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            sources.append(_DoorSource(path, None, None))
            continue
        except OSError as exc:
            problems.append(
                _problem(
                    "door",
                    path.as_posix(),
                    type(exc).__name__,
                    "repair the exact enclosure contract before closeout admission",
                )
            )
            sources.append(_DoorSource(path, None, None))
            continue
        if not stat.S_ISREG(mode):
            problems.append(
                _problem(
                    "door",
                    path.as_posix(),
                    "enclosure-contract-nonregular",
                    "repair the exact enclosure contract before closeout admission",
                )
            )
            sources.append(_DoorSource(path, None, None))
            continue
        try:
            contract = load_contract(path)
        except (ContractError, OSError, UnicodeError, ValueError) as exc:
            problems.append(
                _problem(
                    "door",
                    path.as_posix(),
                    type(exc).__name__,
                    "repair the exact enclosure contract before closeout admission",
                )
            )
            sources.append(_DoorSource(path, None, None))
            continue
        sources.append(_DoorSource(path, contract, contract.closeout_door))
    return sources


def _door_fact(source: _DoorSource) -> dict[str, object]:
    if source.contract is None:
        return {"address": source.address.as_posix(), "state": "missing"}
    return {
        "address": source.address.as_posix(),
        "state": "present",
        "door": source.door.model_dump(mode="json") if source.door is not None else None,
    }


def _problem(
    kind: Literal["task", "door", "series", "projection"],
    address: str,
    error_type: str,
    repair: str,
) -> ProjectionSourceProblem:
    missing = any(marker in error_type for marker in ("missing", "not-found"))
    invalid = (
        any(marker in error_type for marker in ("invalid", "mismatch", "conflict", "cap-exceeded"))
        or error_type == "sprint-required"
    )
    return ProjectionSourceProblem(
        kind=kind,
        address=address,
        state="missing" if missing else "invalid" if invalid else "unreadable",
        errorType=error_type,
        repairAction=repair,
    )


def _bounded_problems(
    problems: list[ProjectionSourceProblem],
    sprint_ref: TaskDocumentRef,
) -> list[ProjectionSourceProblem]:
    unique: list[ProjectionSourceProblem] = []
    seen: set[str] = set()
    for problem in problems:
        key = problem.model_dump_json()
        if key not in seen:
            seen.add(key)
            unique.append(problem)
    if len(unique) <= MAX_CLOSEOUT_SOURCE_PROBLEMS:
        return unique
    overflow = _problem(
        "projection",
        sprint_ref.key,
        "source-problem-cap-exceeded",
        "repair the reported canonical sources, then rebuild",
    )
    return [*unique[: MAX_CLOSEOUT_SOURCE_PROBLEMS - 1], overflow]


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
