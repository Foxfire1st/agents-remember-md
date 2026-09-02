"""Read and write Agents Remember series and leaf enclosure contracts.

The contract is a small markdown file with a limited YAML-like front matter block.
This parser intentionally supports only the subset the workflow writes: scalar
top-level fields and one-level nested sections.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast, get_args

from agents_remember.controlplane.durable_store import SCHEMA_VERSION, schema_version_supported
from agents_remember.errors import AgentsRememberError, TaskIntentError
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration
from agents_remember.models.task_intent import require_task_intent_identity
from agents_remember.models.worktree import (
    CleanupStatus,
    CloseoutStatus,
    HumanReviewStatus,
    IntegrationStatus,
    MemoryMode,
    WorkflowKind,
)
from agents_remember.worktrees.leaf_refs import (
    LeafRefResolutionError,
    canonical_leaf_doc_ids,
    resolve_leaf_ref,
)
from agents_remember.worktrees.task_resolver import (
    TaskResolutionError,
    iter_leaf_enclosure_contracts,
    leaf_enclosure_path,
    resolve_active_task_root,
    series_contract_path,
    slugify,
    task_root_for,
)

logger = logging.getLogger(__name__)

CONTRACT_SCHEMA = "ar-series-contract/v1"
CONTRACT_SCHEMA_VERSION = SCHEMA_VERSION
"""The durable-record contract version stamped into the front matter (260731-EFA-L5 R6).

Deliberately the SAME constant the control-plane JSONL records carry, and read back through the
same :func:`schema_version_supported` rule -- an unknown major is rejected, an unknown minor is
accepted -- because two version policies in one tree is how they drift apart. ``schema`` above
names the document vocabulary; this versions the durable-record contract it is written under.
A contract written before this field existed has no ``schemaVersion`` line and is 1.0 by
definition: that is what "telling an old record from a new one" means here, and it is why no
migration is needed.
"""

# The persisted contract vocabularies, declared once, here. This is where each of these
# values is born and where it dies: `worktree_start` writes the file, the lifecycle tools
# rewrite it, and `models.worktree` imports these same aliases for the response boundary.
# One declaration is what makes "a writer emits a value the wire model rejects" a type
# error at the writer rather than a pydantic ValidationError escaping an MCP tool handler
# with no `except` anywhere on the path (that is the failure 165 of the 213 contracts on
# disk were reproducing: `cleanup: reopened` and `workflow_kind: chat-task` are written by
# `worktrees/reopen.py` and by `worktree_start`'s own documented argument, and neither was in
# the Literal the packet validated against).
#

# The runtime half of each alias, derived from it rather than retyped beside it, so a
# member can only ever be added in one place.
VALID_WORKFLOW_KINDS: frozenset[WorkflowKind] = frozenset(get_args(WorkflowKind))
VALID_MEMORY_MODES: frozenset[MemoryMode] = frozenset(get_args(MemoryMode))
VALID_HUMAN_REVIEW_STATUSES: frozenset[HumanReviewStatus] = frozenset(get_args(HumanReviewStatus))
VALID_CLOSEOUT_STATUSES: frozenset[CloseoutStatus] = frozenset(get_args(CloseoutStatus))
VALID_INTEGRATION_STATUSES: frozenset[IntegrationStatus] = frozenset(get_args(IntegrationStatus))
VALID_CLEANUP_STATUSES: frozenset[CleanupStatus] = frozenset(get_args(CleanupStatus))
VALID_KINDS = {"series", "leaf"}

# The value each vocabulary cell is read as when the file does not say. One declaration
# each: the dataclass defaults below are these constants, and so are the read-path
# fallbacks, so "absent" and "unreadable" can never drift into meaning two things.
# `light-task` mirrors `worktree_start`'s own default argument.
DEFAULT_WORKFLOW_KIND: WorkflowKind = "light-task"
DEFAULT_HUMAN_REVIEW_STATUS: HumanReviewStatus = "pending-review"
DEFAULT_CLOSEOUT_STATUS: CloseoutStatus = "not-started"
DEFAULT_INTEGRATION_STATUS: IntegrationStatus = "not-started"
DEFAULT_CLEANUP_STATUS: CleanupStatus = "pending"


class ContractError(AgentsRememberError):
    """Raised when a worktree contract cannot be parsed or validated."""


def _scalar(value: object) -> str:
    """One front-matter cell's text, or ``""`` when the file does not carry one.

    ``_parse_limited_yaml`` reads a line that is only ``key:`` as the *opening of a section*
    and stores an empty dict, so a cell a developer blanked out arrives here as ``{}``.
    ``str()`` would turn that into the literal ``"{}"`` and hand a two-character token to the
    vocabulary check; an emptied cell is an absent cell, and this is where that is decided.
    """
    return value.strip() if isinstance(value, str) else ""


def _vocabulary_cell[Cell: str](
    raw: str,
    vocabulary: frozenset[Cell],
    field_name: str,
    fallback: Cell,
    quarantined: list[str],
) -> Cell:
    """One parsed front-matter cell, narrowed onto the vocabulary it must belong to.

    Total on purpose. The contract file is untrusted input -- hand-edited, written by an
    older build, produced by a future one -- and a token this vocabulary does not hold is
    not a reason to make the task unreachable. Every lifecycle tool loads the contract
    through :func:`load_contract` and *none* of them catches ``ContractError``, so raising
    here would take ``worktree_closeout_apply``, ``worktree_integrate``, ``worktree_cleanup``,
    ``worktree_sync`` and ``worktree_abandon`` down with it: the developer would be left
    holding a task that no tool can close, integrate, clean up or even abandon.

    So an unreadable cell degrades to ``fallback`` -- the same value an absent cell reads as,
    which is the conservative end of every one of these vocabularies -- and the raw token is
    recorded in ``quarantined`` so nothing is lost silently. The record travels on
    ``WorktreeContract.unknown_cells``, is logged by :func:`load_contract`, and is reported
    on the status payload and the context packet. The recovery is then the ordinary
    lifecycle: any tool that rewrites the contract regenerates the document from this model
    and the file heals to the value already in force.

    The *write* boundary stays closed: :func:`validate_contract` refuses to persist a cell
    outside its vocabulary, so nothing in this package can create the state this tolerates.
    """
    value = raw.strip()
    if not value:
        return fallback
    if value in vocabulary:
        return cast(Cell, value)
    quarantined.append(f"{field_name}={value!r} read as {fallback!r}")
    return fallback


def _memory_mode_fallback(memory: dict[str, str]) -> MemoryMode:
    """What an unreadable ``memory_mode`` degrades to, read off the section it governs.

    The one cell whose fallback cannot be a constant: ``internal`` and ``external`` are not
    two shades of the same state, they decide whether there is a second repository to commit
    and a ledger to map. Guessing ``internal`` for a contract that owns a memory worktree
    would make closeout skip work that exists. The memory section itself already answers the
    question -- a recorded worktree or ledger *is* an external topology, and ``state:
    disabled`` is written by ``default_contract`` for a disabled one -- so the degrade reads
    the facts rather than picking a default.
    """
    if _scalar(memory.get("state")) == "disabled":
        return "disabled"
    if _scalar(memory.get("worktree")) or _scalar(memory.get("ledger")):
        return "external"
    return "internal"


def _task_vocabulary(task: ContractTask) -> tuple[WorkflowKind, MemoryMode]:
    """Narrow a contract *request* onto the persisted vocabulary.

    ``ContractTask`` carries what a caller asked for, and both of these arrive as
    unvalidated ``str`` from ``worktree_start``'s MCP signature, so this is where a request
    becomes a typed record. Both contract factories funnel through it, which is also why
    the memory-mode check is no longer written out twice.

    Strict where the read path is tolerant, and the asymmetry is the point: a *file* holding
    an unknown token is a fact to degrade around, while a *caller* asking for one is a
    mistake to refuse before it is written down. ``build_start_contract`` turns this refusal
    into a blocked ``worktree_start`` result rather than letting it leave the tool handler.
    """
    if task.memory_mode not in VALID_MEMORY_MODES:
        raise ContractError(f"memory_mode must be one of {sorted(VALID_MEMORY_MODES)}")
    if task.workflow_kind not in VALID_WORKFLOW_KINDS:
        raise ContractError(f"workflow_kind must be one of {sorted(VALID_WORKFLOW_KINDS)}")
    return (cast(WorkflowKind, task.workflow_kind), cast(MemoryMode, task.memory_mode))


@dataclass(frozen=True)
class ContractCells:
    """The six persisted vocabulary cells, as one typed record: what a contract write moves.

    Every field is optional and means "leave this one alone" when omitted, so a caller states
    only the cells it moves. All six live here rather than in four places, because the one
    thing that must not happen to this set is for a member of it to be written somewhere the
    others are not.
    """

    workflow_kind: WorkflowKind | None = None
    memory_mode: MemoryMode | None = None
    human_review_status: HumanReviewStatus | None = None
    closeout_status: CloseoutStatus | None = None
    integration_status: IntegrationStatus | None = None
    cleanup: CleanupStatus | None = None


def amend_contract(contract: WorktreeContract, cells: ContractCells) -> WorktreeContract:
    """Move a contract's vocabulary cells, with their types visible to the type checker.

    The lifecycle tools amend a contract with ``dataclasses.replace``, and typeshed declares
    that ``def replace(obj, /, **changes: Any)``. One ``Any`` in a third-party stub is enough
    to void the guarantee this module is built on: ``replace(contract, cleanup="reclaimed-ish")``
    is *zero* pyright errors, even though ``WorktreeContract.cleanup`` is a four-member
    ``Literal`` and the wire model that reports it rejects everything else. Measured: a bare
    off-vocabulary literal at every one of the six fields, through ``replace``, produced no
    diagnostic at all. Declaring them as :class:`ContractCells` fields is what puts them back
    in front of the checker -- pyright now rejects the wrong literal, the concatenation, the
    f-string, the dict subscript, the imported constant and the untyped local, at the call
    site. (``cast`` still passes, as it must: it is the programmer overriding the checker on
    purpose. ``test_wire_vocabulary_exhaustiveness`` is what closes that one, together with
    the rule that no ``replace`` call anywhere may carry one of these keywords.)

    ``replace`` is still what performs the copy -- the values reaching it have already been
    narrowed by ``ContractCells``, which is the whole difference. No member of any of these
    vocabularies is falsy, so ``or`` is the whole of "was I given one".
    """
    return replace(
        contract,
        workflow_kind=cells.workflow_kind or contract.workflow_kind,
        memory_mode=cells.memory_mode or contract.memory_mode,
        human_review_status=cells.human_review_status or contract.human_review_status,
        closeout_status=cells.closeout_status or contract.closeout_status,
        integration_status=cells.integration_status or contract.integration_status,
        cleanup=cells.cleanup or contract.cleanup,
    )


@dataclass(frozen=True)
class WorktreeContract:
    task_id: str
    task_name: str
    repo_name: str
    workflow_kind: WorkflowKind
    memory_mode: MemoryMode
    coordination_root: Path
    task_root: Path
    contract_path: Path
    task_artifact: Path
    worktree_group: Path
    code_repo_path: Path
    code_source_branch: str
    code_work_branch: str
    code_base_commit: str
    code_worktree: Path
    memory_repo_path: Path | None = None
    memory_source_branch: str = ""
    memory_work_branch: str = ""
    memory_base_commit: str = ""
    memory_worktree: Path | None = None
    ledger_path: Path | None = None
    memory_state: str = ""
    human_review_status: HumanReviewStatus = DEFAULT_HUMAN_REVIEW_STATUS
    approved_for_commit: bool = False
    commit_approval_note: str = ""
    closeout_status: CloseoutStatus = DEFAULT_CLOSEOUT_STATUS
    code_commit: str = ""
    memory_content_commit: str = ""
    ledger_commit: str = ""
    integration_status: IntegrationStatus = DEFAULT_INTEGRATION_STATUS
    integration_strategy: str = ""
    integrated_code_commit: str = ""
    integrated_memory_content_commit: str = ""
    integrated_ledger_commit: str = ""
    cleanup: CleanupStatus = DEFAULT_CLEANUP_STATUS
    kind: str = "leaf"
    leaf_id: str = ""
    parent_task_name: str = ""
    parent_contract_path: Path | None = None
    # Durable declaration/disposition authority. Queue membership is derived elsewhere.
    closeout_door: CloseoutDoorGeneration | None = None
    # The lifecycle this enclosure anchors (design §1.1): written by worktree_start
    # promotion, read by worktree_attach to resume. Additive on schema v1 -- old
    # contracts parse to "" (the v2 schema flip is the deliberate 3.0 cutover).
    lifecycle_id: str = ""
    # Mid-task base syncs (issue #54): one entry per worktree_sync that advanced
    # the recorded base pair. A real dataclass field because the closeout/contract
    # rewrite regenerates the document from this model — freeform contract prose
    # does not survive.
    sync_log: tuple[dict[str, str], ...] = field(default=())
    # Cells the file carried that their vocabulary does not hold, as read:
    # "<field>=<raw token> read as <fallback>". Populated only by the read path
    # (`_contract_from_data`); deliberately NOT written back by `contract_to_text`, because a
    # rewrite heals the document to the value already in force everywhere else. Reported on
    # the status payload and the context packet so the substitution is never silent.
    unknown_cells: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class RepoBranchPlan:
    """One repository's branch plan for a worktree pair.

    The contract's ``code:`` and ``memory:`` sections carry exactly these four facts, and
    ``start_contract`` derives them per side as a unit, so they travel together everywhere.
    On the series contract the pair reads ``protected_branch``/``integration_branch``;
    those were only other names for the same fork point and landing branch.
    """

    repo_path: Path
    source_branch: str = ""
    work_branch: str = ""
    base_commit: str = ""


@dataclass(frozen=True)
class ContractTask:
    """The task a contract speaks for.

    Its name, the repository it changes, the coordination tree that holds it, how it is
    run, and the contract one level up (a leaf's series, a series' enclosing task).
    """

    name: str
    repo_name: str
    coordination_root: Path
    workflow_kind: str
    memory_mode: str
    parent_task_name: str = ""
    parent_contract_path: Path | None = None


@dataclass(frozen=True)
class LeafIdentity:
    """Which leaf a leaf-enclosure contract is for.

    The worktree it names, the task-doc id it is filed under (defaulting to the slugified
    worktree name), and the lifecycle the enclosure anchors.
    """

    worktree_name: str
    leaf_id: str | None = None
    lifecycle_id: str = ""


def worktree_folder_name(worktree_name: str) -> str:
    slug = slugify(worktree_name)
    return slug if slug.endswith("-ar") else f"{slug}-ar"


def worktree_group_for(coordination_root: Path, repo_name: str, worktree_name: str) -> Path:
    return coordination_root / "worktrees" / repo_name / worktree_folder_name(worktree_name)


def default_contract(
    task: ContractTask,
    *,
    leaf: LeafIdentity,
    code: RepoBranchPlan,
    memory: RepoBranchPlan | None = None,
) -> WorktreeContract:
    workflow_kind, memory_mode = _task_vocabulary(task)
    task_id = slugify(task.name).upper()
    task_root = resolve_active_task_root(task.coordination_root, task.repo_name, task.name)
    leaf_ref = leaf.leaf_id or leaf.worktree_name
    persisted_leaf = leaf.leaf_id or slugify(leaf.worktree_name)
    contract_path = leaf_enclosure_path(task_root, leaf_ref)
    task_artifact = task_root / "task.md"
    worktree_group = worktree_group_for(task.coordination_root, task.repo_name, leaf.worktree_name)
    code_worktree = worktree_group / slugify(leaf.worktree_name)
    memory_worktree = (
        worktree_group / f"memory-{slugify(leaf.worktree_name)}"
        if task.memory_mode == "external"
        else None
    )
    ledger_path = memory_worktree / "memory.md" if memory_worktree else None
    return WorktreeContract(
        kind="leaf",
        task_id=task_id,
        task_name=task.name,
        repo_name=task.repo_name,
        workflow_kind=workflow_kind,
        memory_mode=memory_mode,
        coordination_root=task.coordination_root,
        task_root=task_root,
        contract_path=contract_path,
        task_artifact=task_artifact,
        worktree_group=worktree_group,
        code_repo_path=code.repo_path,
        code_source_branch=code.source_branch,
        code_work_branch=code.work_branch,
        code_base_commit=code.base_commit,
        code_worktree=code_worktree,
        memory_repo_path=memory.repo_path if memory is not None else None,
        memory_source_branch=memory.source_branch if memory is not None else "",
        memory_work_branch=memory.work_branch if memory is not None else "",
        memory_base_commit=memory.base_commit if memory is not None else "",
        memory_worktree=memory_worktree,
        ledger_path=ledger_path,
        memory_state="disabled" if task.memory_mode == "disabled" else "",
        lifecycle_id=leaf.lifecycle_id,
        leaf_id=persisted_leaf,
        parent_task_name=task.parent_task_name,
        parent_contract_path=task.parent_contract_path,
    )


def default_series_contract(
    task: ContractTask,
    *,
    code: RepoBranchPlan,
    memory: RepoBranchPlan | None = None,
    task_root: Path | None = None,
) -> WorktreeContract:
    workflow_kind, memory_mode = _task_vocabulary(task)
    task_id = slugify(task.name).upper()
    task_root = task_root or task_root_for(task.coordination_root, task.repo_name, task.name)
    contract_path = series_contract_path(task_root)
    memory_repo_path = memory.repo_path if memory is not None else None
    memory_ledger = memory_repo_path / "memory.md" if memory_repo_path else None
    return WorktreeContract(
        kind="series",
        task_id=task_id,
        task_name=task.name,
        repo_name=task.repo_name,
        workflow_kind=workflow_kind,
        memory_mode=memory_mode,
        coordination_root=task.coordination_root,
        task_root=task_root,
        contract_path=contract_path,
        task_artifact=task_root / "task.md",
        worktree_group=worktree_group_for(task.coordination_root, task.repo_name, task.name),
        code_repo_path=code.repo_path,
        code_source_branch=code.source_branch,
        code_work_branch=code.work_branch,
        code_base_commit=code.base_commit,
        code_worktree=code.repo_path,
        memory_repo_path=memory_repo_path,
        memory_source_branch=memory.source_branch if memory is not None else "",
        memory_work_branch=memory.work_branch if memory is not None else "",
        memory_base_commit=memory.base_commit if memory is not None else "",
        ledger_path=memory_ledger,
        parent_task_name=task.parent_task_name,
        parent_contract_path=task.parent_contract_path,
    )


def load_contract(path: Path) -> WorktreeContract:
    """Read + parse + validate one contract file — deliberately walk-free (260712-PTS-L1).

    The read path performs zero tasks-tree traversal: no leaf-ref resolution, no
    series-contract iteration, no glob. A legacy stem-shaped ``leaf_id`` is returned
    verbatim; normalization is a write-time concern (``write_contract``) plus the
    explicit :func:`heal_contract_leaf_ids` migration for contracts already on disk.

    Refuses a document that is not a contract -- no front matter, an unknown schema, a
    missing required field, an external-memory contract with no memory repository -- because
    there is nothing there to act on. It does *not* refuse a contract whose vocabulary cells
    it cannot read: those degrade (see :func:`_vocabulary_cell`) and are logged here, once
    per parse, naming the file that carries them.

    Every one of those refusals names ``path``, and that is why it is threaded down rather
    than left here: this is the single entry point of all six worktree tools, so whichever
    one the developer ran, the refusal it surfaces is the only thing they get -- and a
    refusal that says a field is missing without saying which file to open sends them
    hunting through a tasks tree for it.
    """
    if not path.exists():
        raise ContractError(f"worktree contract does not exist: {path}")
    contract = parse_contract_text(path.read_text(encoding="utf-8"), path=path)
    if contract.unknown_cells:
        logger.warning(
            "worktree contract %s carries %d cell(s) outside their vocabulary: %s",
            path.as_posix(),
            len(contract.unknown_cells),
            "; ".join(contract.unknown_cells),
        )
    return contract


def parse_contract_text(text: str, *, path: Path) -> WorktreeContract:
    """Parse exact retained contract bytes without inventing a temporary-file authority."""

    front_matter = _extract_front_matter(text, path)
    data = _parse_limited_yaml(front_matter)
    contract = _contract_from_data(data, path)
    validate_contract(contract, path=path)
    return contract


def contract_publication_text(path: Path, contract: WorktreeContract) -> str:
    """Return the exact canonical UTF-8 text that :func:`write_contract` publishes."""

    normalized = normalize_contract_leaf_id(contract)
    validate_contract(normalized, path=path)
    _require_publishable_closeout_door(normalized, path)
    return contract_to_text(normalized)


def write_contract(path: Path, contract: WorktreeContract) -> None:
    atomic_write_text(path, contract_publication_text(path, contract))


def heal_contract_leaf_ids(coordination_root: Path, *, dry_run: bool = False) -> dict[str, object]:
    """Heal legacy stem-shaped leaf ids across the active leaf-enclosure population.

    The deliberate, one-shot successor to the per-read normalization ``load_contract``
    used to run (260712-PTS-L1): walk ``tasks/`` once via
    ``iter_leaf_enclosure_contracts`` — the exact population the projection readers
    consume — map each legacy ``leaf_id`` to its canonical doc id through the same
    resolution the read path used to apply (``normalize_contract_leaf_id``), and
    rewrite only the contracts whose id actually changes. Idempotent and cheap on
    re-run: a contract whose ``leaf_id`` already is a doc id of the task root its
    enclosure lives in is skipped via a per-root index (one bounded ``*.json`` scan,
    cached per root) without any resolution walk. Loud by design: every rewrite logs
    one line here and lands in the returned report; an unresolvable or malformed
    entry is reported, never fatal to the sweep. Never invoked implicitly by any
    read path — reach it through the ``heal-leaf-ids`` CLI command or call it
    directly (e.g. once at daemon startup).
    """
    healed: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    canonical = 0
    unchanged = 0
    doc_id_index: dict[Path, frozenset[str]] = {}
    for path in iter_leaf_enclosure_contracts(coordination_root / "tasks"):
        try:
            contract = load_contract(path)
        except (ContractError, OSError) as error:
            errors.append({"contract": path.as_posix(), "error": str(error)})
            continue
        if contract.kind != "leaf" or not contract.leaf_id:
            unchanged += 1
            continue
        # The task root the enclosure PHYSICALLY lives in (enclosures/<leaf>/series-contract.md),
        # not the recorded one: a stale recorded root must degrade to the slow path, not a skip.
        task_root = path.parent.parent.parent
        try:
            known = doc_id_index.get(task_root)
            if known is None:
                known = canonical_leaf_doc_ids(contract.repo_name, task_root)
                doc_id_index[task_root] = known
            if contract.leaf_id in known:
                canonical += 1
                continue
            healed_contract = normalize_contract_leaf_id(contract, keep_unresolved=True)
        except Exception as error:  # one malformed task doc never aborts the sweep
            errors.append({"contract": path.as_posix(), "error": str(error)})
            continue
        if healed_contract.leaf_id == contract.leaf_id:
            unchanged += 1
            continue
        logger.info(
            "heal_contract_leaf_ids: %s leaf_id %r -> %r%s",
            path.as_posix(),
            contract.leaf_id,
            healed_contract.leaf_id,
            " (dry run)" if dry_run else "",
        )
        if not dry_run:
            try:
                write_contract(path, healed_contract)
            except Exception as error:  # a failed rewrite is reported, never fatal to the sweep
                errors.append({"contract": path.as_posix(), "error": str(error)})
                continue
        healed.append(
            {
                "contract": path.as_posix(),
                "from": contract.leaf_id,
                "to": healed_contract.leaf_id,
            }
        )
    return {
        "healed": healed,
        "canonical": canonical,
        "unchanged": unchanged,
        "errors": errors,
        "dryRun": dry_run,
    }


def normalize_contract_leaf_id(
    contract: WorktreeContract, *, keep_unresolved: bool = False
) -> WorktreeContract:
    """Map legacy stem-shaped leaf ids to doc ids when the task tree proves the mapping."""

    if contract.kind != "leaf" or not contract.leaf_id:
        return contract
    try:
        resolved = resolve_leaf_ref(
            contract.coordination_root,
            contract.repo_name,
            contract.leaf_id,
            task_name=contract.task_name,
            parent_task=contract.parent_task_name or None,
        )
    except LeafRefResolutionError:
        return contract
    except TaskResolutionError:
        if keep_unresolved:
            return contract
        raise
    if resolved.doc_id == contract.leaf_id:
        return contract
    return replace(contract, leaf_id=resolved.doc_id)


def _memory_lines(contract: WorktreeContract) -> list[str]:
    lines = [
        "memory:",
        f"  mode: {contract.memory_mode}",
    ]
    if contract.memory_repo_path is not None:
        lines.append(f"  repo_path: {contract.memory_repo_path.as_posix()}")
    if contract.memory_source_branch:
        lines.append(f"  source_branch: {contract.memory_source_branch}")
    if contract.memory_work_branch:
        lines.append(f"  work_branch: {contract.memory_work_branch}")
    if contract.memory_base_commit:
        lines.append(f"  base_commit: {contract.memory_base_commit}")
    if contract.memory_worktree is not None:
        lines.append(f"  worktree: {contract.memory_worktree.as_posix()}")
    if contract.ledger_path is not None:
        lines.append(f"  ledger: {contract.ledger_path.as_posix()}")
    if contract.memory_state:
        lines.append(f"  state: {contract.memory_state}")
    return lines


def _sync_lines(contract: WorktreeContract) -> list[str]:
    if not contract.sync_log:
        return []
    # One JSON scalar keeps the limited front-matter parser (scalar one-level
    # sections only) able to round-trip the log.
    return [
        "sync:",
        f"  log: {json.dumps(list(contract.sync_log), separators=(',', ':'))}",
        "",
    ]


def _parse_sync_log(value: str) -> tuple[dict[str, str], ...]:
    if not value:
        return ()
    try:
        entries = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(entries, list):
        return ()
    return tuple(entry for entry in entries if isinstance(entry, dict))


def _parse_closeout_door(value: str, contract_path: Path) -> CloseoutDoorGeneration | None:
    if not value:
        return None
    try:
        return CloseoutDoorGeneration.model_validate(json.loads(value))
    except (json.JSONDecodeError, ValueError) as error:
        raise ContractError(
            f"invalid contract-owned closeout door (in {contract_path}): {error}"
        ) from error


def _human_review_lines(contract: WorktreeContract, approved: str) -> list[str]:
    lines = [
        "human_review:",
        f"  status: {contract.human_review_status}",
        f"  approved_for_commit: {approved}",
    ]
    if contract.commit_approval_note:
        lines.append(f"  commit_approval_note: {contract.commit_approval_note}")
    return lines


def _closeout_lines(contract: WorktreeContract) -> list[str]:
    lines = [
        "closeout:",
        f"  status: {contract.closeout_status}",
    ]
    if contract.code_commit:
        lines.append(f"  code_commit: {contract.code_commit}")
    if contract.memory_content_commit:
        lines.append(f"  memory_content_commit: {contract.memory_content_commit}")
    if contract.ledger_commit:
        lines.append(f"  ledger_commit: {contract.ledger_commit}")
    return lines


def _integration_lines(contract: WorktreeContract) -> list[str]:
    lines = [
        "integration:",
        f"  status: {contract.integration_status}",
    ]
    if contract.integration_strategy:
        lines.append(f"  strategy: {contract.integration_strategy}")
    if contract.integrated_code_commit:
        lines.append(f"  code_commit: {contract.integrated_code_commit}")
    if contract.integrated_memory_content_commit:
        lines.append(f"  memory_content_commit: {contract.integrated_memory_content_commit}")
    if contract.integrated_ledger_commit:
        lines.append(f"  ledger_commit: {contract.integrated_ledger_commit}")
    lines.append(f"  cleanup: {contract.cleanup}")
    return lines


def _contract_body_lines(contract: WorktreeContract, approved: str) -> list[str]:
    lines = [
        f"# Series Contract - {contract.task_id}",
        "",
        "## Wrapped Workflow",
        "",
        f"Artifact: `{contract.task_artifact.as_posix()}`",
        "",
        "## Human Review State",
        "",
        f"- Status: {contract.human_review_status}",
        f"- Approved for commit: {approved}",
    ]
    if contract.commit_approval_note:
        lines.append(f"- Commit approval note: {contract.commit_approval_note}")
    lines.append("")
    return lines


def contract_to_text(contract: WorktreeContract) -> str:
    approved = "yes" if contract.approved_for_commit else "no"
    lines = [
        "---",
        f"schema: {CONTRACT_SCHEMA}",
        f"schemaVersion: {CONTRACT_SCHEMA_VERSION}",
        f"kind: {contract.kind}",
        f"task_id: {contract.task_id}",
        f"task_name: {contract.task_name}",
        f"repo_name: {contract.repo_name}",
        f"workflow_kind: {contract.workflow_kind}",
        f"memory_mode: {contract.memory_mode}",
        "",
        "coordination:",
        f"  root: {contract.coordination_root.as_posix()}",
        f"  task_root: {contract.task_root.as_posix()}",
        f"  series_contract_path: {contract.contract_path.as_posix()}",
        f"  task_artifact: {contract.task_artifact.as_posix()}",
        f"  worktree_group: {contract.worktree_group.as_posix()}",
        f"  leaf_id: {contract.leaf_id}",
    ]
    if contract.parent_task_name:
        lines.append(f"  parent_task_name: {contract.parent_task_name}")
    if contract.parent_contract_path is not None:
        lines.append(f"  parent_contract_path: {contract.parent_contract_path.as_posix()}")
    if contract.closeout_door is not None:
        encoded_door = json.dumps(
            contract.closeout_door.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        lines.append(f"  closeout_door: {encoded_door}")
    lines.extend(
        [
            "",
            "code:",
            f"  repo_path: {contract.code_repo_path.as_posix()}",
            f"  source_branch: {contract.code_source_branch}",
            f"  work_branch: {contract.code_work_branch}",
            f"  base_commit: {contract.code_base_commit}",
            f"  worktree: {contract.code_worktree.as_posix()}",
            "",
            "lifecycle:",
            f"  id: {contract.lifecycle_id}",
            "",
            *_memory_lines(contract),
            "",
            *_sync_lines(contract),
            *_human_review_lines(contract, approved),
            "",
            *_closeout_lines(contract),
            "",
            *_integration_lines(contract),
            "---",
            "",
            *_contract_body_lines(contract, approved),
        ]
    )
    return "\n".join(lines)


def _contract_vocabularies(
    contract: WorktreeContract,
) -> tuple[tuple[str, str, frozenset[str]], ...]:
    """Every persisted cell as ``(name, value, vocabulary)``, for the write gate.

    The names are the ones ``contract_to_text`` writes into the front matter, so the refusal
    message points at the line a developer would edit.
    """
    return (
        ("workflow_kind", contract.workflow_kind, VALID_WORKFLOW_KINDS),
        ("memory mode", contract.memory_mode, VALID_MEMORY_MODES),
        ("human_review status", contract.human_review_status, VALID_HUMAN_REVIEW_STATUSES),
        ("closeout status", contract.closeout_status, VALID_CLOSEOUT_STATUSES),
        ("integration status", contract.integration_status, VALID_INTEGRATION_STATUSES),
        ("cleanup", contract.cleanup, VALID_CLEANUP_STATUSES),
    )


def validate_contract(contract: WorktreeContract, *, path: Path) -> None:
    """The write gate: what may be persisted as a contract.

    Strict where :func:`load_contract` is tolerant, and that asymmetry is deliberate. The
    reader degrades an unknown cell because refusing one strands a live task in a state no
    lifecycle tool can move; the *writer* refuses one because nothing in this package should
    ever be the thing that put it on disk. Between them, an off-vocabulary cell can only
    arrive from outside -- a hand edit, an older build, a future one -- and can only leave.

    Reached from :func:`write_contract` and from :func:`load_contract` (where the vocabulary
    loop is unreachable by construction, the reader having narrowed every cell already).

    ``path`` is the file the refusal is about -- the one being read, or the one about to be
    written -- and is required rather than defaulted because there is no caller that does not
    have it and no refusal here that reads without it. It is passed in rather than taken from
    ``contract.contract_path`` on purpose: that field is what the *document* claims about
    itself, and a contract that was copied or moved claims the path it came from, which is the
    one file the reader must not be sent to.

    Two message shapes, applied here and everywhere else in this module. A refusal about the
    file as a whole ends with it, which is what :func:`load_contract`'s "does not exist"
    already did: ``<problem>: {path}``. A refusal about something *inside* the file names that
    something first and the file after it: ``<problem>: <detail> (in {path})`` -- so the detail
    a developer greps for stays exactly where it was.
    """
    missing = [
        name
        for name, value in {
            "task_id": contract.task_id,
            "task_name": contract.task_name,
            "repo_name": contract.repo_name,
            "workflow_kind": contract.workflow_kind,
            "memory_mode": contract.memory_mode,
            "code_source_branch": contract.code_source_branch,
            "code_work_branch": contract.code_work_branch,
            "code_base_commit": contract.code_base_commit,
        }.items()
        if not value
    ]
    if missing:
        raise ContractError(f"contract missing required fields: {', '.join(missing)} (in {path})")
    if contract.kind not in VALID_KINDS:
        raise ContractError(f"invalid contract kind: {contract.kind} (in {path})")
    for name, value, vocabulary in _contract_vocabularies(contract):
        if value not in vocabulary:
            raise ContractError(f"invalid {name}: {value} (in {path})")
    if contract.kind == "leaf" and not contract.leaf_id:
        raise ContractError(f"leaf contract missing leaf_id (in {path})")
    if contract.memory_mode == "external" and contract.kind == "leaf":
        for name, value in {
            "memory_repo_path": contract.memory_repo_path,
            "memory_worktree": contract.memory_worktree,
            "ledger_path": contract.ledger_path,
        }.items():
            if value is None:
                raise ContractError(f"external-memory contract missing {name} (in {path})")


def _require_publishable_closeout_door(contract: WorktreeContract, path: Path) -> None:
    if contract.closeout_door is None:
        return
    try:
        require_task_intent_identity(
            contract.closeout_door.taskIntent,
            owner="closeout-door",
            next_action="closeout_door.update-provenance",
        )
    except TaskIntentError as exc:
        raise ContractError(f"{exc.status}: {exc.detail} (in {path})") from exc


def _extract_front_matter(text: str, path: Path) -> str:
    """The front-matter block, or the refusal that this file is not a contract at all.

    Both refusals are about the document as a whole, so both end with the file rather than
    the ``series-contract.md`` they used to name: that constant is the filename the workflow
    writes, not the path the reader was handed, and printing it told a developer only what
    they already knew.
    """
    if not text.startswith("---\n"):
        raise ContractError(f"worktree contract must start with a front matter block: {path}")
    end = text.find("\n---", 4)
    if end == -1:
        raise ContractError(f"worktree contract front matter is not closed: {path}")
    return text[4:end]


def _parse_limited_yaml(text: str) -> dict[str, object]:
    data: dict[str, object] = {}
    current_section: dict[str, str] | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.startswith("  "):
            if current_section is None or ":" not in raw_line:
                continue
            key, value = raw_line.strip().split(":", 1)
            current_section[key.strip()] = value.strip()
            continue
        current_section = None
        if raw_line.endswith(":"):
            section: dict[str, str] = {}
            data[raw_line[:-1].strip()] = section
            current_section = section
            continue
        if ":" in raw_line:
            key, value = raw_line.split(":", 1)
            data[key.strip()] = value.strip()
    return data


def _section(data: dict[str, object], name: str) -> dict[str, str]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _path(value: str, field: str, contract_path: Path) -> Path:
    """One required path cell. ``field`` is its front-matter line, ``section.key``.

    The refusal used to name neither, which left "required contract path field is empty" as
    the whole of what a developer was told about a tree of contracts with six such cells --
    and ``repo_path`` and ``worktree`` each appear under both ``code:`` and ``memory:``, so
    the section is part of the answer, not decoration.
    """
    if not value:
        raise ContractError(f"required contract path field is empty: {field} (in {contract_path})")
    return Path(value)


def _optional_path(value: str) -> Path | None:
    return Path(value) if value else None


def _require_supported_schema_version(data: dict[str, object], contract_path: Path) -> None:
    """Reject a contract whose ``schemaVersion`` major this build does not implement.

    An absent version is a contract written before the field existed, which is 1.0 -- accepted.
    An unknown MINOR is accepted (additive). An unknown MAJOR is refused rather than parsed
    optimistically: the cells would still read, and that is exactly the failure mode -- a
    document that means something else answering questions as though it did not.
    """
    raw = _scalar(data.get("schemaVersion"))
    if raw and not schema_version_supported(raw):
        raise ContractError(
            f"unsupported series contract schemaVersion: {raw} (in {contract_path}); "
            f"this build implements {CONTRACT_SCHEMA_VERSION}"
        )


@dataclass(frozen=True)
class _ParsedVocabulary:
    """The six vocabulary cells a contract file carries, each narrowed onto its Literal.

    ``ContractCells`` is the *amendment* shape -- every field optional, because an
    amendment names only the cells it changes. This is the *parse* shape: every cell has a
    value, because one that was blank or unreadable has already degraded to its declared
    default and left a quarantine record behind.
    """

    workflow_kind: WorkflowKind
    memory_mode: MemoryMode
    human_review_status: HumanReviewStatus
    closeout_status: CloseoutStatus
    integration_status: IntegrationStatus
    cleanup: CleanupStatus


def _parsed_vocabulary(data: dict[str, object], quarantined: list[str]) -> _ParsedVocabulary:
    """Read the six vocabulary cells, appending to ``quarantined`` for each one that failed.

    All six read the same way, with one rule and no exceptions: blank is the declared
    default, a member is itself, anything else is the default plus a quarantine record.
    `workflow_kind` used to be the odd one out -- no `or <default>`, so a cell a developer
    had emptied was a hard refusal where its five siblings were tolerant, on a file the
    packet was perfectly able to report.
    """
    memory = _section(data, "memory")
    human_review = _section(data, "human_review")
    closeout = _section(data, "closeout")
    integration = _section(data, "integration")
    return _ParsedVocabulary(
        workflow_kind=_vocabulary_cell(
            _scalar(data.get("workflow_kind")),
            VALID_WORKFLOW_KINDS,
            "workflow_kind",
            DEFAULT_WORKFLOW_KIND,
            quarantined,
        ),
        memory_mode=_vocabulary_cell(
            _scalar(data.get("memory_mode")) or _scalar(memory.get("mode")),
            VALID_MEMORY_MODES,
            "memory_mode",
            _memory_mode_fallback(memory),
            quarantined,
        ),
        human_review_status=_vocabulary_cell(
            _scalar(human_review.get("status")),
            VALID_HUMAN_REVIEW_STATUSES,
            "human_review status",
            DEFAULT_HUMAN_REVIEW_STATUS,
            quarantined,
        ),
        closeout_status=_vocabulary_cell(
            _scalar(closeout.get("status")),
            VALID_CLOSEOUT_STATUSES,
            "closeout status",
            DEFAULT_CLOSEOUT_STATUS,
            quarantined,
        ),
        integration_status=_vocabulary_cell(
            _scalar(integration.get("status")),
            VALID_INTEGRATION_STATUSES,
            "integration status",
            DEFAULT_INTEGRATION_STATUS,
            quarantined,
        ),
        cleanup=_vocabulary_cell(
            _scalar(integration.get("cleanup")) or _scalar(closeout.get("cleanup")),
            VALID_CLEANUP_STATUSES,
            "cleanup",
            DEFAULT_CLEANUP_STATUS,
            quarantined,
        ),
    )


def _contract_from_data(data: dict[str, object], contract_path: Path) -> WorktreeContract:
    if data.get("schema") != CONTRACT_SCHEMA:
        raise ContractError(
            f"unsupported series contract schema: {data.get('schema', '')} (in {contract_path})"
        )
    _require_supported_schema_version(data, contract_path)
    coordination = _section(data, "coordination")
    code = _section(data, "code")
    memory = _section(data, "memory")
    sync = _section(data, "sync")
    human_review = _section(data, "human_review")
    closeout = _section(data, "closeout")
    integration = _section(data, "integration")
    lifecycle = _section(data, "lifecycle")
    # Every cell this parse could not read, collected as it goes and carried on the result.
    quarantined: list[str] = []
    vocabulary = _parsed_vocabulary(data, quarantined)
    path = (
        _optional_path(coordination.get("series_contract_path", ""))
        or _optional_path(coordination.get("enclosure_path", ""))
        or _optional_path(coordination.get("contract_path", ""))
        or contract_path
    )
    contract = WorktreeContract(
        kind=_scalar(data.get("kind")) or "leaf",
        task_id=_scalar(data.get("task_id")),
        task_name=_scalar(data.get("task_name")),
        repo_name=_scalar(data.get("repo_name")),
        workflow_kind=vocabulary.workflow_kind,
        memory_mode=vocabulary.memory_mode,
        coordination_root=_path(coordination.get("root", ""), "coordination.root", contract_path),
        task_root=_path(coordination.get("task_root", ""), "coordination.task_root", contract_path),
        contract_path=path,
        task_artifact=_path(
            coordination.get("task_artifact", ""), "coordination.task_artifact", contract_path
        ),
        worktree_group=_path(
            coordination.get("worktree_group", ""), "coordination.worktree_group", contract_path
        ),
        code_repo_path=_path(code.get("repo_path", ""), "code.repo_path", contract_path),
        code_source_branch=code.get("source_branch", ""),
        code_work_branch=code.get("work_branch", ""),
        code_base_commit=code.get("base_commit", ""),
        code_worktree=_path(code.get("worktree", ""), "code.worktree", contract_path),
        memory_repo_path=_optional_path(memory.get("repo_path", "")),
        memory_source_branch=memory.get("source_branch", ""),
        memory_work_branch=memory.get("work_branch", ""),
        memory_base_commit=memory.get("base_commit", ""),
        memory_worktree=_optional_path(memory.get("worktree", "")),
        ledger_path=_optional_path(memory.get("ledger", "")),
        memory_state=memory.get("state", ""),
        human_review_status=vocabulary.human_review_status,
        approved_for_commit=human_review.get("approved_for_commit", "no").lower()
        in {"yes", "true", "1"},
        commit_approval_note=human_review.get("commit_approval_note", ""),
        closeout_status=vocabulary.closeout_status,
        code_commit=closeout.get("code_commit", ""),
        memory_content_commit=closeout.get("memory_content_commit", ""),
        ledger_commit=closeout.get("ledger_commit", ""),
        integration_status=vocabulary.integration_status,
        integration_strategy=integration.get("strategy", ""),
        integrated_code_commit=integration.get("code_commit", ""),
        integrated_memory_content_commit=integration.get("memory_content_commit", ""),
        integrated_ledger_commit=integration.get("ledger_commit", ""),
        cleanup=vocabulary.cleanup,
        leaf_id=coordination.get("leaf_id", ""),
        parent_task_name=coordination.get("parent_task_name", ""),
        parent_contract_path=_optional_path(coordination.get("parent_contract_path", "")),
        closeout_door=_parse_closeout_door(coordination.get("closeout_door", ""), contract_path),
        lifecycle_id=lifecycle.get("id", ""),
        sync_log=_parse_sync_log(sync.get("log", "")),
    )
    return replace(contract, unknown_cells=tuple(quarantined)) if quarantined else contract
