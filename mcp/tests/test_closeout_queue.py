"""Shared L3 closeout source/projection fixture and projection-only smoke tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from agents_remember.kernel.memory_ledger import (
    create_initial_ledger,
    load_ledger,
    prepend_mapping,
    write_ledger,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.door import CloseoutDoorAction, CloseoutDoorRequest
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import TaskDocument, read_task_doc, write_task_doc
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.worktrees.integration.closeout.door_control import (
    DoorActor,
    closeout_door_tool,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.queue.closeout_queue import (
    CloseoutQueueRequest,
    QueueActor,
    closeout_queue_tool,
)
from agents_remember.worktrees.route_review import build_route_review
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    contract_publication_text,
    default_contract,
    default_series_contract,
    load_contract,
    write_contract,
)
from curator_coherence_test_support import write_curator_evidence
from test_worktree_support import (
    TEST_CERTIFICATION_PROFILE_REFERENCE,
    git,
    init_repo,
    install_fixture_profile,
)

REPO = "repo-a"
SPRINT = TaskDocumentRef(repository=REPO, path="sprint/task.json")
MASTER_A = TaskDocumentRef(repository=REPO, path="master-a/task.json")
MASTER_B = TaskDocumentRef(repository=REPO, path="master-b/task.json")
LEAF_A = TaskDocumentRef(repository=REPO, path="master-a/leaf-a.json")
LEAF_B = TaskDocumentRef(repository=REPO, path="master-b/leaf-b.json")
NOW = "2026-08-15T00:00:00+00:00"
RATIONALE = "Explicit portfolio judgment."
JUDGMENT_HEADING = "Judgment Register (canonical judgment authority)"
PRIORITY_HEADING = "Priority Register (explicit judgment)"
JUDGMENT_HEADER = (
    "| Judgment id | Kind (dependency meaning, execution nature, blast radius, priority, "
    "blocker placement, reprioritization, or leaf move) | Subject | Decision | Rationale | "
    "Evidence/fact refs | Author | Confidence | Supersedes |"
)
JUDGMENT_SEPARATOR = "| " + " | ".join("---" for _ in range(9)) + " |"
PRIORITY_HEADER = (
    "| Candidate/master | Grade (critical, high, normal, or low) | Affected dependents | "
    "Judgment id |"
)
PRIORITY_SEPARATOR = "| " + " | ".join("---" for _ in range(4)) + " |"


def _master(
    ref: TaskDocumentRef,
    leaf_id: str,
    nature: str,
    *,
    status: str = "inProgress",
) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": ref.path.split("/")[0].upper(),
            "slug": ref.path.split("/")[0],
            "title": ref.path.split("/")[0],
            "kind": "master",
            "status": status,
            "repo": REPO,
            "createdAt": NOW,
            "executionNature": nature,
            "subTasks": [
                {
                    "number": leaf_id,
                    "name": leaf_id,
                    "file": f"{leaf_id.lower()}.md",
                    "status": "inProgress",
                }
            ],
        }
    )


def _leaf(contract: WorktreeContract, slug: str) -> TaskDocument:
    report = contract.task_root / "notes" / "reports" / f"{slug}-review.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Review\n\nPass.\n", encoding="utf-8")
    payload: dict[str, object] = {
        "id": contract.leaf_id,
        "slug": slug,
        "title": slug,
        "kind": "subTask",
        "status": "inProgress",
        "repo": REPO,
        "createdAt": NOW,
        "enclosures": [
            {
                "leafId": contract.leaf_id,
                "enclosurePath": contract.contract_path.as_posix(),
            }
        ],
        "steps": [{"id": "S1", "title": "Ready", "status": "done"}],
    }
    document = TaskDocument.model_validate(payload)
    ref = TaskDocumentRef(
        repository=REPO,
        path=f"{contract.task_name}/{slug}.json",
    )
    review = build_route_review(
        contract,
        ResolvedTaskDocument(
            ref=ref,
            path=contract.task_root / f"{slug}.json",
            document=document,
        ),
        {
            "verdict": "pass",
            "verdictRef": f"notes/reports/{slug}-review.md",
            "routes": [
                {
                    "route": slug,
                    "verdict": "pass",
                    "evidenceRef": f"notes/reports/{slug}-review.md",
                }
            ],
        },
        now=datetime.fromisoformat(NOW),
    )
    return document.model_copy(update={"routeReview": review})


def _judgment_row(ref: TaskDocumentRef, priority: str) -> str:
    return (
        f"| J-{Path(ref.path).stem}-{priority} | priority | {ref.key} | "
        f"priority={priority} | {RATIONALE} | grade.md | "
        "orchestrator | high | |"
    )


def _priority_row(ref: TaskDocumentRef, priority: str) -> str:
    return f"| {ref.key} | {priority} | none | J-{Path(ref.path).stem}-{priority} |"


def _judgment_table(rows: list[str]) -> str:
    return "\n".join([JUDGMENT_HEADER, JUDGMENT_SEPARATOR, *rows])


def _priority_table(rows: list[str]) -> str:
    return "\n".join([PRIORITY_HEADER, PRIORITY_SEPARATOR, *rows])


def _grade(priority: str, candidate: TaskDocumentRef) -> dict[str, Any]:
    return {
        "priority": priority,
        "judgmentId": f"J-{Path(candidate.path).stem}-{priority}",
    }


class QueueFixture:
    """Current task/door/projection fixture retained for non-queue lifecycle suites."""

    def __init__(
        self,
        root: Path,
        *,
        edge: bool = False,
        atomic_a: bool = False,
        atomic_b: bool = False,
        memory_mode: str = "external",
    ) -> None:
        self.root = root
        self.coord = root / "coordination"
        self.tasks = self.coord / "tasks" / REPO
        self.code = root / "code"
        self.memory = self.coord / "memory-repos" / f"ar-{REPO}"
        self.memory_mode = memory_mode
        self.atomic_a = atomic_a
        self.atomic_b = atomic_b
        init_repo(self.code, "main")
        install_fixture_profile(self.code, REPO)
        git(self.code, "add", "-A")
        git(self.code, "commit", "-m", "Add repository certification profile")
        code_base = git(self.code, "rev-parse", "HEAD")
        memory_content = init_repo(self.memory, "main")
        (root / REPO).symlink_to(self.code, target_is_directory=True)
        if memory_mode == "internal":
            (self.code / "ar-memory").mkdir()
        self.config_path = root / "settings.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "coordinationRoot": self.coord.as_posix(),
                    "workspaceRoot": root.as_posix(),
                    "directExecutionEnabled": False,
                    "repositories": {
                        REPO: {
                            "certificationProfile": TEST_CERTIFICATION_PROFILE_REFERENCE.as_posix()
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        write_ledger(
            self.memory / "memory.md",
            create_initial_ledger(REPO, code_base, memory_content),
        )
        git(self.memory, "add", "memory.md")
        git(self.memory, "commit", "-m", "Add ledger")
        memory_base = git(self.memory, "rev-parse", "HEAD")
        git(self.code, "branch", "super", code_base)
        git(self.memory, "branch", "super", memory_base)
        atomic_leaf_ref = TaskDocumentRef(
            repository=REPO,
            path="master-b/leaf-b.json",
        )
        self.leaf_refs = {MASTER_A: LEAF_A, MASTER_B: atomic_leaf_ref}
        self.master_docs = {
            MASTER_A: _master(
                MASTER_A,
                "LEAF-A",
                "atomic" if atomic_a else "organizational",
            ),
            MASTER_B: _master(
                MASTER_B,
                "LEAF-B",
                "atomic" if atomic_b else "organizational",
            ),
        }

        for ref, document in self.master_docs.items():
            write_task_doc(self.tasks / Path(ref.path).parent, document)
        graph = {
            "nodes": [MASTER_A.model_dump(), MASTER_B.model_dump()],
            "edges": (
                [
                    {
                        "predecessor": MASTER_A.model_dump(),
                        "successor": MASTER_B.model_dump(),
                        "reason": "A supplies B.",
                    }
                ]
                if edge
                else []
            ),
        }
        rows = [
            _judgment_row(candidate, priority)
            for candidate in self.leaf_refs.values()
            for priority in ("low", "normal", "critical")
        ]
        self.priorities = {candidate: "normal" for candidate in self.leaf_refs.values()}
        write_task_doc(
            self.tasks / "sprint",
            TaskDocument.model_validate(
                {
                    "id": "SPRINT",
                    "slug": "sprint",
                    "title": "Sprint",
                    "kind": "master",
                    "status": "inProgress",
                    "repo": REPO,
                    "createdAt": NOW,
                    "orchestrates": ["master-a", "master-b"],
                    "integrationBranch": "super",
                    "executionGraph": graph,
                    "sections": [
                        {
                            "kind": "freeform",
                            "heading": JUDGMENT_HEADING,
                            "body": _judgment_table(rows),
                        },
                        {
                            "kind": "freeform",
                            "heading": PRIORITY_HEADING,
                            "body": _priority_table(
                                [_priority_row(*item) for item in self.priorities.items()]
                            ),
                        },
                    ],
                }
            ),
        )
        (self.tasks / "sprint" / "grade.md").write_text("# Grade\n", encoding="utf-8")
        # Curator coherence binds the exact leaf, master, and sprint topology. Publish all
        # structural parents before creating either leaf contract so the fixture never asks the
        # lifecycle API to infer a not-yet-authored task hierarchy.
        self.contracts = {
            MASTER_A: self._contract("master-a", "LEAF-A", code_base, memory_base),
            MASTER_B: self._contract("master-b", "LEAF-B", code_base, memory_base),
        }
        self.cfg = load_config(self.config_path)

    def enable_direct_execution(self) -> None:
        """Opt this fixture into the explicit direct-series policy boundary."""

        settings = json.loads(self.config_path.read_text(encoding="utf-8"))
        settings["directExecutionEnabled"] = True
        self.config_path.write_text(json.dumps(settings), encoding="utf-8")
        self.cfg = load_config(self.config_path)

    def _contract(
        self,
        master: str,
        leaf_id: str,
        code_base: str,
        memory_base: str,
    ) -> WorktreeContract:
        atomic = (self.atomic_a and master == "master-a") or (
            self.atomic_b and master == "master-b"
        )
        code_source = f"ar/{master}" if atomic else "super"
        memory_source = f"ar/{master}" if atomic else "super"
        code_work = f"ar/{leaf_id.lower()}"
        memory_work = f"ar/{leaf_id.lower()}"
        if atomic:
            git(self.code, "branch", code_source, "super")
            git(self.memory, "branch", memory_source, "super")
        task = ContractTask(
            name=master,
            repo_name=REPO,
            coordination_root=self.coord,
            workflow_kind="light-task",
            memory_mode=self.memory_mode,
            parent_task_name="sprint" if atomic else "",
        )
        memory_plan = (
            RepoBranchPlan(
                repo_path=self.memory,
                source_branch="super" if atomic else "main",
                work_branch=memory_source,
                base_commit=memory_base,
            )
            if self.memory_mode == "external"
            else None
        )
        parent = default_series_contract(
            task,
            code=RepoBranchPlan(
                repo_path=self.code,
                source_branch="super" if atomic else "main",
                work_branch=code_source,
                base_commit=code_base,
            ),
            memory=memory_plan,
        )
        published_parent = parent if atomic else replace(parent, cleanup="completed")
        write_contract(parent.contract_path, published_parent)
        publish_new_lifecycle_operation_location(
            published_parent,
            contract_text=contract_publication_text(
                published_parent.contract_path,
                published_parent,
            ),
        )
        contract = default_contract(
            task,
            leaf=LeafIdentity(worktree_name=leaf_id.lower(), leaf_id=leaf_id),
            code=RepoBranchPlan(
                repo_path=self.code,
                source_branch=code_source,
                work_branch=code_work,
                base_commit=code_base,
            ),
            memory=(
                RepoBranchPlan(
                    repo_path=self.memory,
                    source_branch=memory_source,
                    work_branch=memory_work,
                    base_commit=memory_base,
                )
                if self.memory_mode == "external"
                else None
            ),
        )
        if atomic:
            contract = replace(
                contract,
                parent_task_name=master,
                parent_contract_path=parent.contract_path,
            )
        git(self.code, "worktree", "add", "-b", code_work, str(contract.code_worktree), code_source)
        (contract.code_worktree / "feature.txt").write_text(f"{leaf_id}\n", encoding="utf-8")
        if contract.memory_worktree is not None:
            git(
                self.memory,
                "worktree",
                "add",
                "-b",
                memory_work,
                str(contract.memory_worktree),
                memory_source,
            )
            (contract.memory_worktree / f"{leaf_id.lower()}.md").write_text(
                f"# {leaf_id}\n",
                encoding="utf-8",
            )
        write_task_doc(contract.task_root, _leaf(contract, leaf_id.lower()))
        write_contract(contract.contract_path, contract)
        publish_new_lifecycle_operation_location(
            contract,
            contract_text=contract_publication_text(contract.contract_path, contract),
        )
        if contract.memory_worktree is not None:
            write_curator_evidence(contract, caller_ref=SPRINT)
        return contract

    def set_priority(self, candidate: TaskDocumentRef, priority: str) -> None:
        self.priorities[candidate] = priority
        path = self.tasks / "sprint" / "task.json"
        sprint = read_task_doc(path)
        sections = [
            section.model_copy(
                update={
                    "body": _priority_table(
                        [_priority_row(ref, value) for ref, value in self.priorities.items()]
                    )
                }
            )
            if section.heading == PRIORITY_HEADING
            else section
            for section in sprint.sections
        ]
        write_task_doc(path.parent, sprint.model_copy(update={"sections": sections}))

    def replace_section_body(self, heading: str, body: str) -> None:
        path = self.tasks / "sprint" / "task.json"
        sprint = read_task_doc(path)
        if heading == JUDGMENT_HEADING and not body.lstrip().startswith(JUDGMENT_HEADER):
            body = _judgment_table(body.splitlines())
        if heading == PRIORITY_HEADING and not body.lstrip().startswith(PRIORITY_HEADER):
            body = _priority_table(body.splitlines())
        sections = [
            section.model_copy(update={"body": body}) if section.heading == heading else section
            for section in sprint.sections
        ]
        write_task_doc(path.parent, sprint.model_copy(update={"sections": sections}))

    def declare(
        self,
        master: TaskDocumentRef,
        *,
        priority: str | None = "normal",
        admission: dict[str, Any] | None = None,
        request_id: str | None = None,
        update_priority: bool = True,
    ) -> dict[str, Any]:
        del request_id
        if priority is None:
            raise ValueError("L3 door declarations require one canonical scheduling grade")
        contract = load_contract(self.contracts[master].contract_path)
        leaf = self.leaf_refs[master]
        if update_priority:
            self.set_priority(leaf, priority)
        if contract.memory_worktree is not None:
            write_curator_evidence(contract, caller_ref=SPRINT)
        request = CloseoutDoorRequest.model_validate(
            {
                "action": "declare",
                "contract_path": contract.contract_path.as_posix(),
                "grade": _grade(priority, leaf),
                "admission": admission or {},
            }
        )
        closeout_door_tool(
            self.cfg,
            request,
            actor=DoorActor(role="manager", task_document_ref=master),
            admitted_contract=contract,
        )
        self.contracts[master] = load_contract(contract.contract_path)
        # Door publication captures the current curator authority. Publishing a successor
        # coherence generation after that boundary would immediately make the door's immutable
        # memory provenance stale, so rebuild from the exact generation just declared.
        return self.rebuild()

    def rebuild(self) -> dict[str, Any]:
        """Recompute the disposable projection after authoritative fixture writes."""

        return closeout_queue_tool(
            self.cfg,
            CloseoutQueueRequest(action="rebuild", sprint_task_document_ref=SPRINT),
            actor=QueueActor(role="orchestrator", task_document_ref=SPRINT),
            now=NOW,
        )

    def mutate(self, action: CloseoutDoorAction, **values: Any) -> dict[str, Any]:
        if action not in {"defer", "resume", "withdraw"}:
            raise ValueError(f"L3 has no queue mutation action {action!r}")
        candidate = values.get("candidate")
        master = next(master for master, leaf in self.leaf_refs.items() if leaf == candidate)
        contract = load_contract(self.contracts[master].contract_path)
        assert contract.closeout_door is not None
        closeout_door_tool(
            self.cfg,
            CloseoutDoorRequest(
                action=action,
                contract_path=contract.contract_path.as_posix(),
                expected_generation_id=contract.closeout_door.generationId,
            ),
            actor=DoorActor(role="orchestrator", task_document_ref=SPRINT),
            admitted_contract=contract,
        )
        self.contracts[master] = load_contract(contract.contract_path)
        return self.status()

    def status(self, actor: QueueActor | None = None) -> dict[str, Any]:
        return closeout_queue_tool(
            self.cfg,
            CloseoutQueueRequest(action="status", sprint_task_document_ref=SPRINT),
            actor=actor or QueueActor(role="orchestrator", task_document_ref=SPRINT),
            now=NOW,
        )

    def close_contract(self, master: TaskDocumentRef) -> WorktreeContract:
        contract = load_contract(self.contracts[master].contract_path)
        assert contract.memory_worktree is not None
        assert contract.ledger_path is not None
        git(contract.code_worktree, "add", "-A")
        git(contract.code_worktree, "commit", "-m", "close code")
        code_commit = git(contract.code_worktree, "rev-parse", "HEAD")
        git(contract.memory_worktree, "add", "-A")
        git(contract.memory_worktree, "commit", "-m", "close memory")
        memory_commit = git(contract.memory_worktree, "rev-parse", "HEAD")
        write_ledger(
            contract.ledger_path,
            prepend_mapping(load_ledger(contract.ledger_path), code_commit, memory_commit),
        )
        git(contract.memory_worktree, "add", "memory.md")
        git(contract.memory_worktree, "commit", "-m", "close ledger")
        closed = replace(
            contract,
            human_review_status="approved",
            approved_for_commit=True,
            closeout_status="completed",
            code_commit=code_commit,
            memory_content_commit=memory_commit,
            ledger_commit=git(contract.memory_worktree, "rev-parse", "HEAD"),
        )
        write_contract(closed.contract_path, closed)
        self.contracts[master] = closed
        return closed


class CloseoutProjectionSurfaceTests(unittest.TestCase):
    def test_queue_request_has_projection_only_actions(self) -> None:
        self.assertEqual(
            CloseoutQueueRequest(action="status", sprint_task_document_ref=SPRINT).action,
            "status",
        )
        with self.assertRaises(ValueError):
            CloseoutQueueRequest.model_validate(
                {"action": "select", "sprint_task_document_ref": SPRINT.model_dump()}
            )

    def test_door_publication_is_the_only_fixture_membership_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = QueueFixture(Path(temporary), memory_mode="internal")
            self.assertEqual(fixture.status()["members"], [])
            projected = fixture.declare(MASTER_A)
            self.assertEqual(len(projected["members"]), 1)
            self.assertEqual(projected["members"][0]["taskDocumentRef"], LEAF_A.model_dump())
