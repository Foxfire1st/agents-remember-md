from __future__ import annotations

import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agents_remember.application.task_docs.task_doc_route_review import (
    _record_route_review_bound,
    _require_route_review_binding,
    _RouteReviewBinding,
)
from agents_remember.application.task_docs.task_doc_tools import (
    TaskDocEdit,
    TaskDocError,
    TaskDocTarget,
    _enforce_route_review_authority,
    _record_route_review,
    task_doc_tool,
)
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks import RouteReviewRecord, TaskDocument, read_task_doc, write_task_doc
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.leaf_doc import TerminalLeafResolutionError
from agents_remember.tasks.store import json_path_for
from agents_remember.worktrees.route_review import (
    RouteReviewError,
    build_route_review,
    code_candidate_tree,
    document_ref,
    require_current_route_review,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
    write_contract,
)
from pydantic import ValidationError
from test_task_document import ApplicationTests, _doc


def _review_candidate(contract, document: TaskDocument) -> ResolvedTaskDocument:
    path = json_path_for(contract.task_root, document)
    return ResolvedTaskDocument(
        ref=document_ref(contract, path),
        path=path,
        document=document,
    )


def _route_review_contract(coord: Path):
    repo = coord / "repo"
    if not (repo / ".git").exists():
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()
    subprocess.run(["git", "branch", "super", "main"], cwd=repo, check=True)
    task_root = coord / "tasks" / "agents-remember" / "review-x"
    write_task_doc(
        task_root,
        TaskDocument.model_validate(
            {
                "id": "REVIEW-MASTER",
                "slug": "task",
                "title": "Review master",
                "kind": "master",
                "repo": "agents-remember",
                "createdAt": "2026-08-13T00:00:00+00:00",
                "executionNature": "organizational",
                "subTasks": [
                    {
                        "number": "REVIEW-X",
                        "name": "Review",
                        "file": "review-x.md",
                        "status": "planning",
                    }
                ],
            }
        ),
    )
    write_task_doc(
        task_root.parent / "review-sprint",
        TaskDocument.model_validate(
            {
                "id": "REVIEW-SPRINT",
                "slug": "task",
                "title": "Review sprint",
                "kind": "master",
                "repo": "agents-remember",
                "createdAt": "2026-08-13T00:00:00+00:00",
                "orchestrates": ["review-x"],
                "integrationBranch": "super",
                "executionGraph": {
                    "nodes": [
                        {
                            "repository": "agents-remember",
                            "path": "review-x/task.json",
                        }
                    ],
                    "edges": [],
                },
            }
        ),
    )
    contract = default_contract(
        ContractTask(
            name="review-x",
            repo_name="agents-remember",
            coordination_root=coord,
            workflow_kind="chat-task",
            memory_mode="disabled",
        ),
        leaf=LeafIdentity(worktree_name="review-x", leaf_id="REVIEW-X", lifecycle_id="LC-REVIEW"),
        code=RepoBranchPlan(
            repo_path=repo, source_branch="super", work_branch="review-x", base_commit=base
        ),
    )
    contract.code_worktree.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            contract.code_work_branch,
            contract.code_worktree.as_posix(),
            contract.code_source_branch,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    write_contract(contract.contract_path, contract)
    return contract


def _organizational_leaf_contract(
    coord: Path,
    *,
    task_name: str,
    leaf_id: str,
    leaf_slug: str,
    lifecycle_id: str,
):
    repo = coord / "repo"
    branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/super"],
        cwd=repo,
        check=False,
    )
    if branch.returncode != 0:
        subprocess.run(["git", "branch", "super", "main"], cwd=repo, check=True)
    task_root = coord / "tasks" / "agents-remember" / task_name
    write_task_doc(
        task_root,
        TaskDocument.model_validate(
            {
                "id": f"{leaf_id}-MASTER",
                "slug": "task",
                "title": f"{task_name} master",
                "kind": "master",
                "repo": "agents-remember",
                "createdAt": "2026-01-01T00:00",
                "executionNature": "organizational",
                "subTasks": [
                    {
                        "number": leaf_id,
                        "name": leaf_id,
                        "file": f"{leaf_slug}.md",
                        "status": "planning",
                    }
                ],
            }
        ),
    )
    write_task_doc(
        task_root.parent / f"{task_name}-sprint",
        TaskDocument.model_validate(
            {
                "id": f"{leaf_id}-SPRINT",
                "slug": "task",
                "title": f"{task_name} sprint",
                "kind": "master",
                "repo": "agents-remember",
                "createdAt": "2026-01-01T00:00",
                "orchestrates": [task_name],
                "integrationBranch": "super",
                "executionGraph": {
                    "nodes": [
                        {
                            "repository": "agents-remember",
                            "path": f"{task_name}/task.json",
                        }
                    ],
                    "edges": [],
                },
            }
        ),
    )
    base = subprocess.run(
        ["git", "rev-parse", "super"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    contract = default_contract(
        ContractTask(
            name=task_name,
            repo_name="agents-remember",
            coordination_root=coord,
            workflow_kind="chat-task",
            memory_mode="disabled",
        ),
        leaf=LeafIdentity(
            worktree_name=leaf_slug,
            leaf_id=leaf_id,
            lifecycle_id=lifecycle_id,
        ),
        code=RepoBranchPlan(
            repo_path=repo,
            source_branch="super",
            work_branch=f"ar/{leaf_slug}",
            base_commit=base,
        ),
    )
    contract.code_worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            contract.code_work_branch,
            contract.code_worktree.as_posix(),
            contract.code_source_branch,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    write_contract(contract.contract_path, contract)
    return contract


class ApplicationTests2(ApplicationTests):
    def test_route_review_contract_initializes_a_fresh_coordination_tree(self) -> None:
        # A fresh coord exercises the git-init path (the shared class fixture already
        # carries the repo, so the init block never runs there).
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp)
            contract = _route_review_contract(fresh)
            self.assertTrue((fresh / "repo" / ".git").exists())
            self.assertEqual(contract.code_repo_path, fresh / "repo")

    def test_master_closeout_does_not_require_leaf_route_review(self) -> None:
        contract = _route_review_contract(self.coord)
        (contract.code_worktree / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")

        with (
            mock.patch(
                "agents_remember.worktrees.route_review.code_change_present"
            ) as candidate_probe,
            mock.patch(
                "agents_remember.worktrees.route_review.resolve_terminal_leaf_doc"
            ) as leaf_probe,
        ):
            self.assertEqual(
                require_current_route_review(replace(contract, kind="series", leaf_id="")),
                {"required": False, "status": "not-required-master-altitude"},
            )

        candidate_probe.assert_not_called()
        leaf_probe.assert_not_called()

    def test_route_review_is_plane_stamped_to_the_current_tree_and_rendered(self) -> None:
        contract = _route_review_contract(self.coord)
        created = task_doc_tool(
            self.cfg,
            TaskDocTarget(
                repo_id="agents-remember", contract_path=contract.contract_path.as_posix()
            ),
            operation="create",
            edit=TaskDocEdit(
                fields={
                    "id": "REVIEW-X",
                    "slug": "review-x",
                    "title": "Review",
                    "repo": "agents-remember",
                    "createdAt": "2026-08-13T00:00:00+00:00",
                }
            ),
        )
        (contract.code_worktree / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
        report = contract.task_root / "notes/reports/reviewer-verdict.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Verdict\n\nPass.\n", encoding="utf-8")
        recorded = task_doc_tool(
            self.cfg,
            TaskDocTarget(
                repo_id="agents-remember",
                contract_path=contract.contract_path.as_posix(),
                slug="review-x",
            ),
            operation="record_route_review",
            edit=TaskDocEdit(
                review={
                    "verdict": "pass",
                    "verdictRef": "notes/reports/reviewer-verdict.md",
                    "routes": [
                        {
                            "route": "worktrees",
                            "verdict": "pass",
                            "evidenceRef": "notes/reports/reviewer-verdict.md",
                        }
                    ],
                }
            ),
        )

        document = read_task_doc(Path(str(created["docPath"])))
        assert document.routeReview is not None
        self.assertEqual(require_current_route_review(contract)["status"], "current")
        self.assertIn(
            f"**Candidate tree:** `{document.routeReview.candidateTree}`",
            Path(str(recorded["renderedPath"])).read_text(encoding="utf-8"),
        )
        (contract.code_worktree / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(RouteReviewError, "candidate changed"):
            require_current_route_review(contract)

    def test_route_review_fails_closed_for_every_untrusted_or_incomplete_state(self) -> None:
        contract = _route_review_contract(self.coord)
        self.assertEqual(
            require_current_route_review(contract),
            {"required": False, "status": "not-required-no-code-change"},
        )
        (contract.code_worktree / "committed.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=contract.code_worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "precommitted candidate"],
            cwd=contract.code_worktree,
            check=True,
            capture_output=True,
        )
        with self.assertRaisesRegex(RouteReviewError, "no task document"):
            require_current_route_review(contract)
        (contract.code_worktree / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(RouteReviewError, "no task document"):
            require_current_route_review(contract)

        bare = _doc(
            id=contract.leaf_id,
            slug=contract.leaf_id,
            kind="subTask",
            repo=contract.repo_name,
            enclosures=[
                {
                    "leafId": contract.leaf_id,
                    "enclosurePath": contract.contract_path.as_posix(),
                }
            ],
        )
        write_task_doc(contract.task_root, bare)
        with self.assertRaisesRegex(RouteReviewError, "no independent route-review"):
            require_current_route_review(contract)

        report = contract.task_root / "notes/reports/verdict.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Verdict\n", encoding="utf-8")
        valid = {
            "verdict": "pass",
            "verdictRef": "notes/reports/verdict.md",
            "routes": [
                {
                    "route": "worktrees",
                    "verdict": "pass",
                    "evidenceRef": "notes/reports/verdict.md",
                }
            ],
        }
        for payload, message in (
            ({**valid, "candidateTree": "0" * 40}, "plane owns candidateTree"),
            ({**valid, "routes": []}, "at least 1 item"),
            ({**valid, "verdictRef": "missing.md"}, "does not exist"),
            ({**valid, "verdictRef": report.as_posix()}, "task-relative"),
            ({**valid, "verdictRef": "../outside.md"}, "escapes the task root"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RouteReviewError, message):
                build_route_review(contract, _review_candidate(contract, bare), payload)
        with self.assertRaisesRegex(RouteReviewError, "belongs to a leaf"):
            build_route_review(
                replace(contract, kind="series"),
                _review_candidate(contract, bare),
                valid,
            )

        blocked_record = build_route_review(
            contract,
            _review_candidate(contract, bare),
            {
                **valid,
                "verdict": "block",
                "routes": [
                    {
                        "route": "worktrees",
                        "verdict": "block",
                        "evidenceRef": "notes/reports/verdict.md",
                    }
                ],
            },
            now=datetime(2026, 8, 13, tzinfo=UTC),
        )
        blocked_doc = bare.model_copy(update={"routeReview": blocked_record})
        write_task_doc(contract.task_root, blocked_doc)
        with self.assertRaisesRegex(RouteReviewError, "blocks this candidate"):
            require_current_route_review(contract)

    def test_route_review_schema_and_task_doc_authority_reject_forgery(self) -> None:
        contract = _route_review_contract(self.coord)
        report = contract.task_root / "notes/reports/verdict.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Verdict\n", encoding="utf-8")
        route = {
            "route": "worktrees",
            "verdict": "pass",
            "evidenceRef": "notes/reports/verdict.md",
        }
        valid = {
            "candidateTree": code_candidate_tree(contract),
            "verdict": "pass",
            "verdictRef": "notes/reports/verdict.md",
            "reviewedAt": "2026-08-13T00:00:00+00:00",
            "routes": [route],
        }
        for mutation in (
            {**valid, "routes": [route, route]},
            {**valid, "verdict": "block"},
            {**valid, "routes": [{**route, "verdict": "block"}]},
            {**valid, "verdictRef": " "},
            {**valid, "routes": [{**route, "route": " "}]},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                RouteReviewRecord.model_validate(mutation)

        bare = _doc(id="REVIEW-X", slug="REVIEW-X", kind="subTask")
        bare_path = json_path_for(contract.task_root, bare)
        reviewed = bare.model_copy(update={"routeReview": RouteReviewRecord.model_validate(valid)})
        master = _doc(id="MASTER", slug="master", kind="master")
        with self.assertRaisesRegex(TaskDocError, "only for a leaf"):
            _record_route_review(master, {}, contract, contract.task_root, bare_path)
        with self.assertRaisesRegex(TaskDocError, "requires the leaf worktree contract"):
            _record_route_review(bare, {}, None, contract.task_root, bare_path)
        with self.assertRaisesRegex(TaskDocError, "requires a review object"):
            _record_route_review(bare, None, contract, contract.task_root, bare_path)
        with self.assertRaises(TaskDocError):
            _record_route_review(bare, {}, contract, contract.task_root, bare_path)
        with (
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.resolve_terminal_leaf_doc",
                return_value=None,
            ),
            self.assertRaisesRegex(TaskDocError, "exact task document"),
        ):
            _record_route_review(bare, valid, contract, contract.task_root, bare_path)
        with (
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.resolve_terminal_leaf_doc",
                return_value=(bare_path, bare),
            ),
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.build_route_review",
                side_effect=RouteReviewError("route-review-invalid", "invalid review"),
            ),
            self.assertRaisesRegex(TaskDocError, "invalid review"),
        ):
            _record_route_review(bare, valid, contract, contract.task_root, bare_path)
        with self.assertRaisesRegex(TaskDocError, "create cannot author"):
            _enforce_route_review_authority("create", None, reviewed)
        with self.assertRaisesRegex(TaskDocError, "replace cannot add"):
            _enforce_route_review_authority("replace", bare, reviewed)

    def test_skip_step_refuses_blank_missing_wrong_parent_and_ambiguity(self) -> None:
        self._create(
            steps=[
                {
                    "id": "S1",
                    "title": "First",
                    "substeps": [{"id": "C1", "title": "Child"}],
                },
                {"id": "S1", "title": "Duplicate"},
            ]
        )
        for step in (
            {"id": "S1", "reason": " "},
            {"id": "missing", "reason": "x"},
            {"id": "C1", "parent": "wrong", "reason": "x"},
            {"id": "S1", "reason": "x"},
        ):
            with self.subTest(step=step), self.assertRaises(TaskDocError):
                self._call("skip_step", step=step)

    def test_skip_step_accepts_each_unresolved_status(self) -> None:
        self._create(
            steps=[
                {"id": status, "title": status, "status": status}
                for status in ("pending", "inProgress", "blocked")
            ]
        )

        for status in ("pending", "inProgress", "blocked"):
            with self.subTest(status=status):
                self._call(
                    "skip_step",
                    step={"id": status, "reason": f"Skip the {status} unit."},
                )

        doc = read_task_doc(self.coord / "tasks" / "agents-remember" / "3c-x" / "03c_x.json")
        self.assertEqual([step.status for step in doc.steps], ["done", "done", "done"])
        self.assertEqual(len(doc.decisions), 3)

    def test_skip_step_refuses_done_units_without_changing_docs_or_audit(self) -> None:
        created = self._create(steps=[{"id": "S1", "title": "One", "status": "done"}])
        json_path = Path(str(created["docPath"]))
        markdown_path = Path(str(created["renderedPath"]))

        for mode in ("ordinary-done", "already-skipped"):
            if mode == "already-skipped":
                self._call("set_step", step={"id": "S1", "status": "pending"})
                self._call("skip_step", step={"id": "S1", "reason": "Original skip."})
            before_json = json_path.read_bytes()
            before_markdown = markdown_path.read_bytes()
            before_decisions = list(read_task_doc(json_path).decisions)

            with self.subTest(mode=mode), self.assertRaises(TaskDocError) as raised:
                self._call("skip_step", step={"id": "S1", "reason": "Second skip."})

            self.assertIn("already done", str(raised.exception))
            self.assertEqual(json_path.read_bytes(), before_json)
            self.assertEqual(markdown_path.read_bytes(), before_markdown)
            self.assertEqual(read_task_doc(json_path).decisions, before_decisions)

    def test_set_step_title_preserves_skip_but_explicit_status_clears_it(self) -> None:
        self._create(steps=[{"id": "S1", "title": "One", "status": "pending"}])
        self._call("skip_step", step={"id": "S1", "reason": "Not needed."})

        renamed = self._call("set_step", step={"id": "S1", "title": "Renamed"})
        renamed_doc = read_task_doc(Path(str(renamed["docPath"])))
        self.assertIsNotNone(renamed_doc.steps[0].disposition)

        executed = self._call("set_step", step={"id": "S1", "status": "done"})
        executed_doc = read_task_doc(Path(str(executed["docPath"])))
        self.assertIsNone(executed_doc.steps[0].disposition)

    def test_create_and_replace_cannot_author_skip_disposition(self) -> None:
        disposition = {
            "kind": "intentionalSkip",
            "reason": "No longer needed.",
            "recordedAt": "2026-08-03T12:00:00+00:00",
            "recordedVia": "task_doc.skip_step",
        }
        with self.assertRaises(TaskDocError):
            self._create(
                steps=[{"id": "S1", "title": "One", "status": "done", "disposition": disposition}]
            )

        self._create(steps=[{"id": "S1", "title": "One", "status": "pending"}])
        self._call("skip_step", step={"id": "S1", "reason": "No longer needed."})
        doc_path = self.coord / "tasks" / "agents-remember" / "3c-x" / "03c_x.json"
        replacement = read_task_doc(doc_path).model_dump(by_alias=True)
        replacement["steps"][0]["disposition"]["reason"] = "Changed out of band."
        with self.assertRaises(TaskDocError):
            self._call("replace", fields=replacement)

    def test_disposition_requires_done_status_for_parent_and_nested_units(self) -> None:
        disposition = {
            "kind": "intentionalSkip",
            "reason": "No longer needed.",
            "recordedAt": "2026-08-03T12:00:00+00:00",
            "recordedVia": "task_doc.skip_step",
        }
        for steps in (
            [
                {
                    "id": "S1",
                    "title": "Parent",
                    "status": "pending",
                    "disposition": disposition,
                }
            ],
            [
                {
                    "id": "S1",
                    "title": "Parent",
                    "status": "done",
                    "substeps": [
                        {
                            "id": "C1",
                            "title": "Child",
                            "status": "blocked",
                            "disposition": disposition,
                        }
                    ],
                }
            ],
        ):
            with self.subTest(steps=steps), self.assertRaises(ValidationError):
                _doc(steps=steps)

    def test_replace_cannot_keep_disposition_while_reopening_parent_or_child(self) -> None:
        self._create(
            steps=[
                {
                    "id": "S1",
                    "title": "Parent",
                    "status": "pending",
                    "substeps": [{"id": "C1", "title": "Child", "status": "pending"}],
                }
            ]
        )
        self._call("skip_step", step={"id": "S1", "reason": "Parent skip."})
        self._call(
            "skip_step",
            step={"id": "C1", "parent": "S1", "reason": "Child skip."},
        )
        doc_path = self.coord / "tasks" / "agents-remember" / "3c-x" / "03c_x.json"
        for target in ("parent", "child"):
            replacement = read_task_doc(doc_path).model_dump(by_alias=True)
            if target == "parent":
                replacement["steps"][0]["status"] = "pending"
            else:
                replacement["steps"][0]["substeps"][0]["status"] = "blocked"
            with self.subTest(target=target), self.assertRaises(TaskDocError):
                self._call("replace", fields=replacement)

    def test_replace_preserves_unresolved_parent_and_qualified_child_identity(self) -> None:
        created = self._create(
            steps=[
                {
                    "id": "S1",
                    "title": "Parent",
                    "status": "pending",
                    "substeps": [{"id": "C1", "title": "Child", "status": "blocked"}],
                }
            ]
        )
        original = read_task_doc(Path(str(created["docPath"]))).model_dump(by_alias=True)
        replacements = []
        dropped_parent = dict(original)
        dropped_parent["steps"] = []
        replacements.append(dropped_parent)
        dropped_child = TaskDocument.model_validate(original).model_dump(by_alias=True)
        dropped_child["steps"][0]["substeps"] = []
        replacements.append(dropped_child)
        renamed_child = TaskDocument.model_validate(original).model_dump(by_alias=True)
        renamed_child["steps"][0]["substeps"][0]["id"] = "C2"
        replacements.append(renamed_child)
        moved_child = TaskDocument.model_validate(original).model_dump(by_alias=True)
        child = moved_child["steps"][0]["substeps"].pop()
        moved_child["steps"].append(
            {"id": "S2", "title": "Other parent", "status": "done", "substeps": [child]}
        )
        replacements.append(moved_child)

        for replacement in replacements:
            with self.subTest(replacement=replacement), self.assertRaises(TaskDocError) as raised:
                self._call("replace", fields=replacement)
            self.assertIn("cannot remove or rename unresolved work units", str(raised.exception))

    def test_replace_preserves_multiplicity_of_duplicate_unresolved_ids(self) -> None:
        created = self._create(
            steps=[
                {"id": "S1", "title": "First", "status": "pending"},
                {"id": "S1", "title": "Second", "status": "blocked"},
            ]
        )
        replacement = read_task_doc(Path(str(created["docPath"]))).model_dump(by_alias=True)
        replacement["steps"].pop()
        with self.assertRaises(TaskDocError) as raised:
            self._call("replace", fields=replacement)
        self.assertIn("['S1']", str(raised.exception))

    def test_replace_cannot_drop_pending_work_before_or_during_completion(self) -> None:
        created = self._create(
            steps=[
                {
                    "id": "S1",
                    "title": "Parent",
                    "status": "done",
                    "substeps": [{"id": "C1", "title": "Child", "status": "pending"}],
                }
            ]
        )
        replacement = read_task_doc(Path(str(created["docPath"]))).model_dump(by_alias=True)
        replacement["steps"][0]["substeps"] = []
        replacement["status"] = "Completed"
        with self.assertRaises(TaskDocError):
            self._call("replace", fields=replacement)

        replacement["status"] = "inProgress"
        with self.assertRaises(TaskDocError):
            self._call("replace", fields=replacement)
        with self.assertRaises(TaskDocError) as terminal:
            self._call("set_status", fields={"status": "Completed"})
        self.assertIn("'id': 'C1'", str(terminal.exception))
        self.assertIn("'parentId': 'S1'", str(terminal.exception))

    def test_completion_paths_refuse_unresolved_nodes_and_allow_all_done(self) -> None:
        with self.assertRaises(TaskDocError):
            self._create(
                status="Completed",
                steps=[{"id": "S1", "title": "One", "status": "pending"}],
            )

        self._create(steps=[{"id": "S1", "title": "One", "status": "pending"}])
        for operation in ("set_status", "set_field"):
            with self.subTest(operation=operation), self.assertRaises(TaskDocError) as raised:
                self._call(operation, fields={"status": "Completed"})
            self.assertIn("'id': 'S1'", str(raised.exception))

        self._call("set_step", step={"id": "S1", "status": "done"})
        completed = self._call("set_status", fields={"status": "Completed"})
        self.assertEqual(completed["status"], "Completed")

    def test_legacy_inconsistent_completed_doc_is_readable_but_every_mutation_is_gated(
        self,
    ) -> None:
        task_root = self.coord / "tasks" / "agents-remember" / "3c-x"
        legacy = _doc(
            id="3C",
            slug="03c_x",
            kind="subTask",
            status="Completed",
            repo="agents-remember",
            steps=[{"id": "S1", "title": "Forgotten", "status": "pending"}],
        )
        json_path, markdown_path = write_task_doc(task_root, legacy)
        self.assertEqual(self._call("get")["status"], "Completed")
        before_json = json_path.read_bytes()
        before_markdown = markdown_path.read_bytes()
        mutations = (
            ("set_field", TaskDocEdit(fields={"objective": "Metadata repair."})),
            (
                "append_decision",
                TaskDocEdit(decision={"at": "t", "decision": "d", "rationale": "r"}),
            ),
            ("set_section", TaskDocEdit(section={"heading": "Note", "body": "repair"})),
        )
        for operation, edit in mutations:
            with self.subTest(operation=operation), self.assertRaises(TaskDocError):
                task_doc_tool(
                    self.cfg,
                    TaskDocTarget(repo_id="agents-remember", task_name="3c-x", slug="03c_x"),
                    operation=operation,
                    edit=edit,
                )
            self.assertEqual(json_path.read_bytes(), before_json)
            self.assertEqual(markdown_path.read_bytes(), before_markdown)
        self.assertEqual(read_task_doc(json_path).status, "Completed")

    def test_ready_completed_doc_allows_metadata_mutations(self) -> None:
        created = self._create(
            status="Completed",
            steps=[{"id": "S1", "title": "Done", "status": "done"}],
        )
        self._call("set_field", fields={"objective": "Metadata repair."})
        self._call(
            "append_decision",
            decision={"at": "t", "decision": "d", "rationale": "r"},
        )
        changed = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x", slug="03c_x"),
            operation="set_section",
            edit=TaskDocEdit(section={"heading": "Status history", "body": "still terminal"}),
        )
        doc = read_task_doc(Path(str(changed["docPath"])))
        self.assertEqual(doc.status, "Completed")
        self.assertEqual(doc.objective, "Metadata repair.")
        self.assertEqual(doc.decisions[-1].decision, "d")
        self.assertEqual(doc.sections[-1].heading, "Status history")
        self.assertEqual(Path(str(created["docPath"])), Path(str(changed["docPath"])))

    def test_terminal_candidate_can_truthfully_resolve_existing_work_in_same_call(self) -> None:
        created = self._create(
            status="inProgress",
            steps=[{"id": "S1", "title": "Forgotten", "status": "pending"}],
        )
        replacement = read_task_doc(Path(str(created["docPath"]))).model_dump(by_alias=True)
        replacement["status"] = "Completed"
        replacement["steps"][0]["status"] = "done"
        replaced = self._call("replace", fields=replacement)
        self.assertEqual(replaced["status"], "Completed")

        task_root = self.coord / "tasks" / "agents-remember" / "legacy-fix"
        legacy = _doc(
            id="LF",
            slug="01_legacy",
            kind="subTask",
            status="Completed",
            repo="agents-remember",
            steps=[{"id": "S1", "title": "Forgotten", "status": "pending"}],
        )
        json_path, _ = write_task_doc(task_root, legacy)
        fixed = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="legacy-fix", slug="01_legacy"),
            operation="set_step",
            edit=TaskDocEdit(step={"id": "S1", "status": "done"}),
        )
        self.assertEqual(fixed["status"], "Completed")
        self.assertEqual(read_task_doc(json_path).steps[0].status, "done")

    def test_append_decision_accumulates(self) -> None:
        self._create()
        self._call("append_decision", decision={"at": "t1", "decision": "d1", "rationale": "r"})
        result = self._call(
            "append_decision", decision={"at": "t2", "decision": "d2", "rationale": "r"}
        )
        self.assertEqual(len(read_task_doc(Path(str(result["docPath"]))).decisions), 2)

    def test_get_does_not_mutate(self) -> None:
        self._create()
        before = Path(str(self._call("get")["docPath"])).read_text(encoding="utf-8")
        after = Path(str(self._call("get")["docPath"])).read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_create_picks_up_contract_lifecycle_id(self) -> None:
        _organizational_leaf_contract(
            self.coord,
            task_name="3c-x",
            leaf_id="3C",
            leaf_slug="03c_x",
            lifecycle_id="LC-CONTRACT",
        )
        result = self._create()  # no lifecycleId in fields
        self.assertEqual(result["lifecycleId"], "LC-CONTRACT")

    def test_organizational_leaf_contract_reuses_an_existing_super_branch(self) -> None:
        _organizational_leaf_contract(
            self.coord,
            task_name="reuse-a",
            leaf_id="RA",
            leaf_slug="01_reuse_a",
            lifecycle_id="LC-REUSE-A",
        )
        # A second call on the same coord finds the super branch already present,
        # exercising the branch-exists path inside the helper.
        second = _organizational_leaf_contract(
            self.coord,
            task_name="reuse-b",
            leaf_id="RB",
            leaf_slug="02_reuse_b",
            lifecycle_id="LC-REUSE-B",
        )
        self.assertEqual(second.code_source_branch, "super")

    def test_create_refuses_light_and_defaults_master_without_contract(self) -> None:
        base = {
            "id": "K1",
            "slug": "task",
            "title": "Kind",
            "repo": "agents-remember",
            "createdAt": "2026-01-01T00:00",
        }
        # Explicit light is refused: every task is wrapped master/leaf, even a single-file change.
        with self.assertRaises(TaskDocError):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(repo_id="agents-remember", task_name="kind-x"),
                operation="create",
                edit=TaskDocEdit(fields={**base, "kind": "light"}),
            )
        # No contract + no kind defaults to a standalone master (not the retired "light" default).
        created = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="kind-x"),
            operation="create",
            edit=TaskDocEdit(fields=base),
        )
        self.assertEqual(created["kind"], "master")
        # replace shares _build_doc, so it refuses light on the same path.
        with self.assertRaises(TaskDocError):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(repo_id="agents-remember", task_name="kind-x", slug="task"),
                operation="replace",
                edit=TaskDocEdit(fields={**base, "kind": "light"}),
            )

    def test_create_defaults_subtask_under_leaf_contract(self) -> None:
        _organizational_leaf_contract(
            self.coord,
            task_name="leaf-x",
            leaf_id="L1",
            leaf_slug="01_leaf",
            lifecycle_id="LC-LEAF",
        )
        # A bare create against a leaf contract is the leaf sub-task (context-aware default).
        result = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="leaf-x"),
            operation="create",
            edit=TaskDocEdit(
                fields={
                    "id": "L1",
                    "slug": "01_leaf",
                    "title": "Leaf",
                    "repo": "agents-remember",
                    "createdAt": "2026-01-01T00:00",
                }
            ),
        )
        self.assertEqual(result["kind"], "subTask")

    def test_resolve_by_contract_path(self) -> None:
        created = self._create()
        task_root = Path(str(created["docPath"])).parent
        result = task_doc_tool(
            self.cfg,
            TaskDocTarget(
                repo_id="agents-remember",
                contract_path=str(task_root / "series-contract.md"),
                slug="03c_x",
            ),
            operation="get",
        )
        self.assertEqual(result["taskId"], "3C")

    def test_error_paths(self) -> None:
        with self.assertRaises(TaskDocError):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(repo_id="agents-remember", task_name="x"),
                operation="frob",
            )
        with self.assertRaises(TaskDocError):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(repo_id="agents-remember"),
                operation="get",
            )
        with self.assertRaises(TaskDocError):
            self._call("get")  # doc not created yet
        self._create()
        with self.assertRaises(TaskDocError):
            self._call("set_status", fields={})
        with self.assertRaises(TaskDocError):
            self._call("set_field", fields={"unknown": "x"})
        with self.assertRaises(TaskDocError):
            self._call("set_step", step={"title": "no id"})
        with self.assertRaises(TaskDocError):
            self._call("set_step", step={"id": "S9.a", "title": "x", "parent": "ghost"})


class RouteReviewBindingSurfaceTests(ApplicationTests):
    """Branch surface of task_doc_route_review.py (gate round 2 rail 3).

    Covers the binding refusals and the bound-form success that the
    task_doc-level tests only reach through mocks, so every changed branch in
    the extracted route-review module runs in the suite.
    """

    def _contract(self):
        return _route_review_contract(self.coord)

    def _leaf(self, contract):
        doc = _doc(
            id=contract.leaf_id,
            slug=contract.leaf_id,
            kind="subTask",
            repo=contract.repo_name,
            enclosures=[
                {
                    "leafId": contract.leaf_id,
                    "enclosurePath": contract.contract_path.as_posix(),
                }
            ],
        )
        write_task_doc(contract.task_root, doc)
        return doc

    def test_bound_form_refuses_master_payload_none_and_build_error(self) -> None:
        contract = self._contract()
        leaf = self._leaf(contract)
        leaf_path = json_path_for(contract.task_root, leaf)
        binding = _RouteReviewBinding(
            contract=contract, task_root=contract.task_root, selected_path=leaf_path
        )
        master = leaf.model_copy(update={"kind": "master"})
        with self.assertRaisesRegex(TaskDocError, "only for a leaf"):
            _record_route_review_bound(master, {"verdict": "pass"}, binding)
        with self.assertRaisesRegex(TaskDocError, "requires a review object"):
            _record_route_review_bound(leaf, None, binding)
        with (
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.resolve_terminal_leaf_doc",
                return_value=(leaf_path, leaf),
            ),
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.build_route_review",
                side_effect=RouteReviewError("route-review-invalid", "invalid review"),
            ),
            self.assertRaisesRegex(TaskDocError, "invalid review"),
        ):
            _record_route_review_bound(leaf, {"verdict": "pass"}, binding)

    def test_require_binding_refuses_each_contract_kind_and_resolution_failure(self) -> None:
        contract = self._contract()
        series = replace(contract, kind="series", leaf_id="")
        root = contract.task_root
        leaf_path = root / "leaf.json"
        with self.assertRaisesRegex(TaskDocError, "requires the leaf worktree contract"):
            _require_route_review_binding(
                _RouteReviewBinding(contract=series, task_root=root, selected_path=leaf_path)
            )
        with self.assertRaisesRegex(TaskDocError, "branch_addressed mode requires"):
            _require_route_review_binding(
                _RouteReviewBinding(
                    contract=contract,
                    task_root=root,
                    selected_path=leaf_path,
                    branch_addressed=True,
                )
            )
        with self.assertRaisesRegex(TaskDocError, "outside the task root"):
            _require_route_review_binding(
                _RouteReviewBinding(
                    contract=series,
                    task_root=root,
                    selected_path=root.parent / "escape" / "leaf.json",
                    branch_addressed=True,
                )
            )
        with (
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.resolve_terminal_leaf_doc",
                side_effect=TerminalLeafResolutionError("no-terminal-leaf", "no terminal leaf"),
            ),
            self.assertRaisesRegex(TaskDocError, "no terminal leaf"),
        ):
            _require_route_review_binding(
                _RouteReviewBinding(contract=contract, task_root=root, selected_path=leaf_path)
            )
        with (
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.resolve_terminal_leaf_doc",
                return_value=None,
            ),
            self.assertRaisesRegex(TaskDocError, "exact task document"),
        ):
            _require_route_review_binding(
                _RouteReviewBinding(contract=contract, task_root=root, selected_path=leaf_path)
            )

    def test_json_primary_success_records_current_route_review(self) -> None:
        contract = self._contract()
        leaf = self._leaf(contract)
        leaf_path = json_path_for(contract.task_root, leaf)
        reports = contract.task_root / "notes/reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "verdict.md").write_text("# Verdict\n", encoding="utf-8")
        (reports / "route-evidence.md").write_text("# Route evidence\n", encoding="utf-8")
        valid = {
            "verdict": "pass",
            "verdictRef": "notes/reports/verdict.md",
            "routes": [
                {
                    "route": "worktrees",
                    "verdict": "pass",
                    "evidenceRef": "notes/reports/route-evidence.md",
                }
            ],
        }
        with (
            mock.patch(
                "agents_remember.application.task_docs.task_doc_route_review.resolve_terminal_leaf_doc",
                return_value=(leaf_path, leaf),
            ),
        ):
            recorded = _record_route_review(
                leaf,
                valid,
                contract,
                contract.task_root,
                leaf_path,
            )
        review = recorded.routeReview
        self.assertIsNotNone(review)
        assert review is not None
        self.assertEqual(review.verdict, "pass")
        assert isinstance(review.taskIntent, TaskIntentIdentity)
        self.assertEqual(review.taskIntent.schema_, "task-intent/v1")
