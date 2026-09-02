from __future__ import annotations

import io
import json
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import agents_remember.providers.provider_setup as provider_setup_api
from agents_remember.kernel import atomic_write, filesystem
from agents_remember.kernel import coordination_context_resolver as resolver
from agents_remember.kernel.coordination_context.models import CoordinationRequest
from agents_remember.kernel.coordination_context_resolver import (
    CoordinationHints,
    EnclosureSelector,
)
from agents_remember.kernel.memory_ledger import (
    LedgerError,
    create_initial_ledger,
    find_mapping,
    load_ledger,
    parse_ledger_text,
    prepend_mapping,
    write_ledger,
)
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.worktrees import git_worktree_manager as worktree_manager
from agents_remember.worktrees.modules import start as start_module
from agents_remember.worktrees.modules.contract_reader import WorktreeContractReader
from agents_remember.worktrees.modules.onboarding import _refresh_regenerated_documents
from agents_remember.worktrees.modules.startup import start_contract
from agents_remember.worktrees.task_resolver import (
    leaf_enclosure_path,
    series_contract_path,
    task_root_candidates,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
    load_contract,
    worktree_group_for,
    write_contract,
)
from test_worktree_support import (
    WorktreeSupportTests,
    closeout_args,
    commit_file,
    dirty_open_external_contract_fixture,
    git,
    init_repo,
    long_path_tempdir,
    long_source_path,
    read_onboarding_field,
    run_authorized_closeout_mechanics,
)


class WorktreeSupport1(WorktreeSupportTests):
    def test_refresh_regenerated_documents_stamps_only_touched_citation_documents(self) -> None:
        """The regenerated-citation refresh: modified metadata docs advance, everything else skips.

        Covers the extension's branches: a stamped document, an already-planned one, an excluded
        overview, a document without verification metadata, and a prose-only field mention whose
        metadata row is not a real row.
        """
        with tempfile.TemporaryDirectory() as tmp:
            memory_repo = Path(tmp) / "memory"
            init_repo(memory_repo, "main")
            onboarding_root = memory_repo / "onboarding"
            onboarding_root.mkdir()

            def write_doc(relative: str, *, metadata: str = "table", stamp: str = "0" * 40) -> Path:
                rows = {
                    "table": f"| lastVerifiedCommitHash | `{stamp}` |\n| lastVerifiedCommitDate | 2026-01-01T00:00:00+00:00 |",
                    "none": "",
                    "prose": "Mentions lastVerifiedCommitHash in prose without a table row.",
                }
                path = onboarding_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {relative}\n\n{rows[metadata]}\n", encoding="utf-8")
                return path

            write_doc("a/touched.py.md")
            write_doc("a/planned.py.md")
            write_doc("overview.md", metadata="none")
            write_doc("a/no_metadata.py.md", metadata="none")
            write_doc("a/prose.py.md", metadata="prose")
            (memory_repo / "memory.md").write_text("# ledger\n", encoding="utf-8")
            git(memory_repo, "add", "-A")
            git(memory_repo, "commit", "-m", "baseline")
            # Touch only some of them after the baseline commit (plus a non-onboarding file).
            (memory_repo / "memory.md").write_text("# ledger\n\nedited\n", encoding="utf-8")
            for relative in (
                "a/touched.py.md",
                "a/planned.py.md",
                "overview.md",
                "a/no_metadata.py.md",
                "a/prose.py.md",
            ):
                path = onboarding_root / relative
                path.write_text(
                    path.read_text(encoding="utf-8") + "\nEdited in the task.\n", encoding="utf-8"
                )

            context = SimpleNamespace(onboarding_root=onboarding_root)
            refreshed = _refresh_regenerated_documents(
                context,
                memory_tree=memory_repo,
                verified_commit="f" * 40,
                verified_date="2026-08-06T00:00:00+02:00",
                already={(onboarding_root / "a/planned.py.md").as_posix()},
            )

            self.assertEqual(len(refreshed), 1)
            self.assertEqual(
                read_onboarding_field(
                    onboarding_root / "a/touched.py.md", "lastVerifiedCommitHash"
                ),
                "f" * 40,
            )
            # Planned, excluded overview, no-metadata, and prose-only documents are untouched.
            self.assertEqual(
                read_onboarding_field(
                    onboarding_root / "a/planned.py.md", "lastVerifiedCommitHash"
                ),
                "0" * 40,
            )

    def test_refresh_regenerated_documents_is_empty_without_a_memory_tree(self) -> None:
        refreshed = _refresh_regenerated_documents(
            SimpleNamespace(onboarding_root=Path("/nonexistent")),
            memory_tree=None,
            verified_commit="f" * 40,
            verified_date="2026-08-06T00:00:00+02:00",
            already=set(),
        )
        self.assertEqual(refreshed, [])

    def test_refresh_regenerated_documents_is_empty_without_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as not_a_repo:
            refreshed = _refresh_regenerated_documents(
                SimpleNamespace(onboarding_root=Path(not_a_repo)),
                memory_tree=Path(not_a_repo),
                verified_commit="f" * 40,
                verified_date="2026-08-06T00:00:00+02:00",
                already=set(),
            )
        self.assertEqual(refreshed, [])

    def test_memory_base_for_source_uses_source_branch_tip_not_head(self) -> None:
        # Regression (L3): worktree_start must record the memory base from the source branch the
        # worktree is created off, NOT the memory repo's current HEAD (which may sit on an unrelated
        # in-flight branch). Reading HEAD recorded a divergent base that broke closeout's
        # "memory source branch moved" preflight.
        with tempfile.TemporaryDirectory() as tmp:
            memory_repo = Path(tmp) / "memory"
            init_repo(memory_repo, "main")
            main_tip = commit_file(memory_repo, "memory.md", "official\n", "official tip")
            git(memory_repo, "checkout", "-b", "other-task")
            head_tip = commit_file(memory_repo, "memory.md", "other\n", "unrelated in-flight work")
            self.assertNotEqual(main_tip, head_tip)
            # repo HEAD is on 'other-task', but the base for a 'main'-sourced worktree is main's tip
            self.assertEqual(start_contract.memory_base_for_source(memory_repo, "main"), main_tip)
            # no source branch (internal/disabled memory) -> falls back to current HEAD
            self.assertEqual(start_contract.memory_base_for_source(memory_repo, ""), head_tip)
            # no memory repo -> empty
            self.assertEqual(start_contract.memory_base_for_source(None, "main"), "")

    def test_master_start_creates_integration_contract_and_leaf_enclosure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code_repo = workspace / "repo-a"
            init_repo(code_repo, "main")
            git(code_repo, "branch", "super", "main")
            coordination_root = workspace / "ar-coordination"
            (coordination_root / "memory-repos" / "ar-repo-a" / "system").mkdir(parents=True)
            (coordination_root / "memory-repos" / "ar-repo-a" / "onboarding").mkdir()
            task_root = coordination_root / "tasks" / "repo-a" / "260624_master"
            write_task_doc(
                coordination_root / "tasks" / "repo-a" / "260624_sprint",
                TaskDocument.model_validate(
                    {
                        "id": "sprint",
                        "slug": "task",
                        "title": "Sprint",
                        "kind": "master",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-06-24T01:00",
                        "orchestrates": ["260624_master"],
                        "integrationBranch": "super",
                        "executionGraph": {
                            "nodes": [
                                {
                                    "repository": "repo-a",
                                    "path": "260624_master/task.json",
                                }
                            ],
                            "edges": [],
                        },
                    }
                ),
            )
            write_task_doc(
                task_root,
                TaskDocument.model_validate(
                    {
                        "id": "master",
                        "slug": "task",
                        "title": "Master Series",
                        "kind": "master",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-06-24T02:00",
                        "executionNature": "atomic",
                        "subTasks": [
                            {
                                "number": "15",
                                "name": "Leaf task",
                                "file": "15_leaf.md",
                                "status": "inProgress",
                            }
                        ],
                    }
                ),
            )
            write_task_doc(
                task_root,
                TaskDocument.model_validate(
                    {
                        "id": "15",
                        "slug": "15_leaf",
                        "title": "Leaf task",
                        "kind": "subTask",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-06-24T02:01",
                        "master": "task.md",
                    }
                ),
            )

            result = worktree_manager.start_result(
                worktree_manager.WorktreeArgs(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    coordination_root=coordination_root,
                    code_repository_root=code_repo,
                    topology="external",
                    task_name="260624_master",
                    worktree_name="15_leaf",
                    leaf_id="15_leaf",
                    workflow_kind="light-task",
                    memory_mode="disabled",
                    skip_provider_setup=True,
                    lifecycle_id="LC-LEAF",
                )
            )

            self.assertEqual(result.returncode, 0)
            root_contract = load_contract(series_contract_path(task_root))
            leaf_contract = load_contract(leaf_enclosure_path(task_root, "15"))
            self.assertEqual(
                (root_contract.kind, root_contract.code_source_branch), ("series", "super")
            )
            self.assertEqual(root_contract.code_work_branch, "ar/260624_master")
            self.assertEqual(root_contract.code_worktree, code_repo)
            self.assertEqual((leaf_contract.kind, leaf_contract.leaf_id), ("leaf", "15"))
            self.assertEqual(leaf_contract.code_source_branch, "ar/260624_master")
            self.assertEqual(leaf_contract.code_work_branch, "ar/15_leaf")
            self.assertEqual(leaf_contract.parent_contract_path, root_contract.contract_path)
            self.assertEqual(
                result.payload["enclosure_path"], leaf_contract.contract_path.as_posix()
            )
            self.assertIn(
                "ar/260624_master", git(code_repo, "branch", "--list", "ar/260624_master")
            )
            self.assertIn("ar/15_leaf", git(code_repo, "branch", "--list", "ar/15_leaf"))

    def test_standalone_light_task_is_refused_without_a_master_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code_repo = workspace / "repo-a"
            init_repo(code_repo, "main")
            coordination_root = workspace / "ar-coordination"
            (coordination_root / "memory-repos" / "ar-repo-a" / "system").mkdir(parents=True)
            (coordination_root / "memory-repos" / "ar-repo-a" / "onboarding").mkdir()
            task_root = coordination_root / "tasks" / "repo-a" / "fix-thing"
            write_task_doc(
                task_root,
                TaskDocument.model_validate(
                    {
                        "id": "260707-T1",
                        "slug": "fix-thing",
                        "title": "Fix Thing",
                        "kind": "light",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-07-07T10:00",
                    }
                ),
            )

            result = worktree_manager.start_result(
                worktree_manager.WorktreeArgs(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    coordination_root=coordination_root,
                    code_repository_root=code_repo,
                    topology="external",
                    task_name="fix thing",
                    worktree_name="fix-thing",
                    memory_mode="disabled",
                    skip_provider_setup=True,
                    lifecycle_id="LC-LIGHT",
                )
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.payload["state"], "blocked")
            self.assertIn("source_lineage", result.payload)
            self.assertFalse(leaf_enclosure_path(task_root, "260707-T1").exists())

    def test_light_task_start_rejects_wrong_default_ref_with_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code_repo = workspace / "repo-a"
            init_repo(code_repo, "main")
            coordination_root = workspace / "ar-coordination"
            (coordination_root / "memory-repos" / "ar-repo-a" / "system").mkdir(parents=True)
            (coordination_root / "memory-repos" / "ar-repo-a" / "onboarding").mkdir()
            task_root = coordination_root / "tasks" / "repo-a" / "fix-thing"
            write_task_doc(
                task_root,
                TaskDocument.model_validate(
                    {
                        "id": "260707-T1",
                        "slug": "fix-thing",
                        "title": "Fix Thing",
                        "kind": "light",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-07-07T10:00",
                    }
                ),
            )

            result = worktree_manager.start_result(
                worktree_manager.WorktreeArgs(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    coordination_root=coordination_root,
                    code_repository_root=code_repo,
                    topology="external",
                    task_name="fix thing",
                    worktree_name="wrong-ref",
                    memory_mode="disabled",
                    skip_provider_setup=True,
                )
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.payload["state"], "leaf-ref-not-found")
            candidates = result.payload.get("candidates")
            assert isinstance(candidates, list)
            self.assertIn("repo-a/fix-thing/260707-T1", candidates)

    def test_memory_ledger_rejects_bad_top_row(self) -> None:
        text = "\n".join(
            [
                "# Memory Ledger",
                "",
                "```json ar-memory-ledger",
                "{",
                '  "schema": "ar-memory-ledger/v1",',
                '  "repoName": "repo-a",',
                '  "baseCodeCommit": "c1",',
                '  "baseMemoryCommit": "m1",',
                '  "lastVerifiedCodeCommit": "c2",',
                '  "lastMemoryContentCommit": "m2",',
                '  "sortOrder": "newest-first"',
                "}",
                "```",
                "",
                "| Code commit | Memory commit |",
                "| ----------- | ------------- |",
                "| c1 | m1 |",
            ]
        )
        with self.assertRaises(LedgerError):
            parse_ledger_text(text)

    def test_memory_ledger_rejects_malformed_metadata(self) -> None:
        text = "\n".join(
            [
                "# Memory Ledger",
                "",
                "```json ar-memory-ledger",
                '{"schema": "ar-memory-ledger/v1",',
                "```",
                "",
                "| Code commit | Memory commit |",
                "| ----------- | ------------- |",
                "| c1 | m1 |",
            ]
        )
        with self.assertRaises(LedgerError):
            parse_ledger_text(text)

    def test_resolver_returns_repo_task_root_without_task_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo = root / "repo-a"
            code_repo.mkdir()
            memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
            (memory_repo / "system").mkdir(parents=True)
            (memory_repo / "onboarding").mkdir()
            (memory_repo / "system" / "settings.md").write_text("# Settings\n", encoding="utf-8")

            context = resolver.resolve_coordination_context(
                code_repository_root=code_repo,
                request=CoordinationRequest(
                    hints=CoordinationHints(
                        topology="external", coordination_root=root / "ar-coordination"
                    ),
                    contract_reader=WorktreeContractReader(),
                ),
            )

            self.assertEqual(context.task_root, root / "ar-coordination" / "tasks" / "repo-a")

    def test_resolver_resolves_contract_by_worktree_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo, coordination_root = self._external_memory_skeleton(root)
            contract = self._write_task_contract(
                coordination_root,
                code_repo,
                task_name="260610_browser-dashboard",
                worktree_name="260610-browser-dashboard",
            )

            context = resolver.resolve_coordination_context(
                code_repository_root=code_repo,
                request=CoordinationRequest(
                    hints=CoordinationHints(
                        topology="external", coordination_root=coordination_root
                    ),
                    selector=EnclosureSelector(worktree_name="260610-browser-dashboard"),
                    contract_reader=WorktreeContractReader(),
                ),
            )

            # Regression guard: contract-derived fields are populated, not blanked.
            self.assertIsNotNone(context.contract_path)
            self.assertIsNotNone(context.code_worktree)
            self.assertIsNotNone(context.memory_worktree)
            self.assertEqual(context.contract_path, contract.contract_path)
            self.assertEqual(context.code_worktree, contract.code_worktree)
            self.assertEqual(context.memory_worktree, contract.memory_worktree)
            self.assertEqual(
                context.worktree_group,
                worktree_group_for(coordination_root, "repo-a", "260610-browser-dashboard"),
            )

    def test_resolver_returns_empty_for_unknown_worktree_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo, coordination_root = self._external_memory_skeleton(root)
            # A real contract exists, but for a different worktree group.
            self._write_task_contract(
                coordination_root,
                code_repo,
                task_name="task-other",
                worktree_name="something-else",
            )

            context = resolver.resolve_coordination_context(
                code_repository_root=code_repo,
                request=CoordinationRequest(
                    hints=CoordinationHints(
                        topology="external", coordination_root=coordination_root
                    ),
                    selector=EnclosureSelector(worktree_name="no-such-worktree"),
                    contract_reader=WorktreeContractReader(),
                ),
            )

            self.assertIsNone(context.contract_path)
            self.assertIsNone(context.code_worktree)
            self.assertIsNone(context.memory_worktree)

    def test_resolver_prefers_task_name_over_worktree_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo, coordination_root = self._external_memory_skeleton(root)
            contract_a = self._write_task_contract(
                coordination_root, code_repo, task_name="task-a", worktree_name="worktree-a"
            )
            contract_b = self._write_task_contract(
                coordination_root, code_repo, task_name="task-b", worktree_name="worktree-b"
            )

            context = resolver.resolve_coordination_context(
                code_repository_root=code_repo,
                request=CoordinationRequest(
                    hints=CoordinationHints(
                        topology="external", coordination_root=coordination_root
                    ),
                    selector=EnclosureSelector(
                        task_name="task-a", leaf_id="worktree-a", worktree_name="worktree-b"
                    ),
                    contract_reader=WorktreeContractReader(),
                ),
            )

            # Task-based resolution (task_name + leaf_id) wins; the worktree_name match (B) is never consulted.
            self.assertEqual(context.contract_path, contract_a.contract_path)
            self.assertEqual(context.code_worktree, contract_a.code_worktree)
            self.assertNotEqual(context.code_worktree, contract_b.code_worktree)

    def test_find_worktree_contract_matches_group_or_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo, coordination_root = self._external_memory_skeleton(root)
            contract = self._write_task_contract(
                coordination_root, code_repo, task_name="task-x", worktree_name="worktree-x"
            )

            found = WorktreeContractReader().find_worktree_contract(
                coordination_root, "repo-a", "worktree-x"
            )
            self.assertEqual(found, contract.contract_path)

            missing = WorktreeContractReader().find_worktree_contract(
                coordination_root, "repo-a", "worktree-y"
            )
            self.assertIsNone(missing)

    def test_find_worktree_contract_skips_archived_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo, coordination_root = self._external_memory_skeleton(root)
            contract = self._write_task_contract(
                coordination_root,
                code_repo,
                task_name="task-archived",
                worktree_name="worktree-archived",
            )
            # Move the (group-matching) contract under 0_archive/: its recorded
            # worktree_group still matches, so only the archive guard should keep
            # find_worktree_contract from resurrecting a retired task.
            tasks_root = coordination_root / "tasks" / "repo-a"
            archived_dir = tasks_root / "0_archive" / "task-archived"
            archived_dir.mkdir(parents=True, exist_ok=True)
            contract.contract_path.rename(archived_dir / contract.contract_path.name)

            found = WorktreeContractReader().find_worktree_contract(
                coordination_root, "repo-a", "worktree-archived"
            )
            self.assertIsNone(found)

    def test_worktree_provider_start_passes_grepai_worktree_memory_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coordination_root = root / "ar-coordination"
            code_repo = root / "repo-a"
            memory_repo = coordination_root / "memory-repos" / "ar-repo-a"
            settings_path = root / "provider-settings.json"
            code_repo.mkdir(parents=True)
            memory_repo.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "contextProviders": {
                            "enabled": True,
                            "providers": {
                                "grepai-memory": {
                                    "enabled": True,
                                    "roots": [
                                        {
                                            "projectId": "repo-a",
                                            "path": memory_repo.as_posix(),
                                        }
                                    ],
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            contract = default_contract(
                ContractTask(
                    name="Provider task",
                    repo_name="repo-a",
                    coordination_root=coordination_root,
                    workflow_kind="light-task",
                    memory_mode="external",
                ),
                leaf=LeafIdentity(worktree_name="provider-task"),
                code=RepoBranchPlan(
                    repo_path=code_repo,
                    source_branch="main",
                    work_branch="ar/provider-task",
                    base_commit="abc123",
                ),
                memory=RepoBranchPlan(
                    repo_path=memory_repo,
                    source_branch="main",
                    work_branch="ar/provider-task",
                    base_commit="def456",
                ),
            )
            context = Namespace(
                code_repository_name="repo-a",
                code_repository_root=code_repo,
                coordination_root=coordination_root,
                memory_root=memory_repo,
            )
            args = worktree_manager.WorktreeArgs(
                dry_run=True,
                skip_provider_setup=False,
                provider_timeout=1,
                provider_setup_config=worktree_manager.WorktreeProviderSetupConfig(
                    coordination_root=coordination_root,
                    settings_path=settings_path,
                    seed_source_coordination_root=coordination_root,
                ),
            )
            captured: dict[str, Any] = {}

            def fake_run_provider_setup(request):
                captured["request"] = request
                return {"ok": True, "results": []}

            with mock.patch.object(
                provider_setup_api,
                "run_provider_setup",
                side_effect=fake_run_provider_setup,
            ):
                payload = worktree_manager.prepare_providers_for_start(context, contract, args)

            self.assertEqual(payload["state"], "planned")
            request = captured["request"]
            self.assertEqual(request.skip_grepai, False)
            self.assertEqual(request.grepai_seed.project_id, "repo-a")
            self.assertEqual(request.grepai_seed.source_coordination_root, coordination_root)
            self.assertEqual(request.grepai_seed.target_memory_root, contract.memory_worktree)
            self.assertEqual(
                request.grepai_isolated.runtime_root, contract.worktree_group / "provider-runtime"
            )
            self.assertEqual(request.grepai_isolated.target_memory_root, contract.memory_worktree)

    def test_start_ignores_legacy_ledger_branch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo = root / "repo-a"
            code_base = init_repo(code_repo, "main")
            memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
            memory_base = init_repo(memory_repo, "main")
            (memory_repo / "memory.md").write_text(
                "\n".join(
                    [
                        "# Memory Branch Ledger",
                        "",
                        "```json ar-memory-ledger",
                        "{",
                        '  "schema": "ar-memory-branch-ledger/v1",',
                        '  "repoName": "repo-a",',
                        '  "trackedCodeBranch": "dev",',
                        '  "memoryBranch": "dev",',
                        f'  "baseCodeCommit": "{code_base}",',
                        f'  "baseMemoryCommit": "{memory_base}",',
                        f'  "lastVerifiedCodeCommit": "{code_base}",',
                        f'  "lastMemoryContentCommit": "{memory_base}",',
                        '  "sortOrder": "newest-first"',
                        "}",
                        "```",
                        "",
                        "Newest entries are always inserted at the top.",
                        "",
                        "| Code commit | Memory commit |",
                        "| ----------- | ------------- |",
                        f"| {code_base} | {memory_base} |",
                    ]
                ),
                encoding="utf-8",
            )
            git(memory_repo, "add", "memory.md")
            git(memory_repo, "commit", "-m", "ledger")
            contract = default_contract(
                ContractTask(
                    name="Fix Thing",
                    repo_name="repo-a",
                    coordination_root=root / "ar-coordination",
                    workflow_kind="light-task",
                    memory_mode="external",
                ),
                leaf=LeafIdentity(worktree_name="fix-thing"),
                code=RepoBranchPlan(
                    repo_path=code_repo,
                    source_branch="main",
                    work_branch="ar/fix-thing",
                    base_commit=code_base,
                ),
                memory=RepoBranchPlan(
                    repo_path=memory_repo,
                    source_branch="main",
                    work_branch="ar/fix-thing",
                    base_commit=memory_base,
                ),
            )
            with mock.patch.object(start_module, "ensure_worktree", return_value="would-create"):
                result: dict[str, Any] = worktree_manager.prepare_memory_for_start(
                    contract, worktree_manager.WorktreeArgs(memory_choice=None, dry_run=True)
                )
            self.assertEqual(result["state"], "compatible")
            self.assertEqual(result["worktree"], "would-create")

    def test_start_blocks_dirty_external_memory_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo = root / "repo-a"
            code_base = init_repo(code_repo, "main")
            memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
            memory_seed = init_repo(memory_repo, "main")
            write_ledger(
                memory_repo / "memory.md",
                create_initial_ledger("repo-a", code_base, memory_seed),
            )
            git(memory_repo, "add", "memory.md")
            git(memory_repo, "commit", "-m", "Add memory ledger")
            (memory_repo / "onboarding" / "fresh.md").parent.mkdir(parents=True, exist_ok=True)
            (memory_repo / "onboarding" / "fresh.md").write_text("# fresh\n", encoding="utf-8")
            contract = default_contract(
                ContractTask(
                    name="Fix Thing",
                    repo_name="repo-a",
                    coordination_root=root / "ar-coordination",
                    workflow_kind="light-task",
                    memory_mode="external",
                ),
                leaf=LeafIdentity(worktree_name="fix-thing"),
                code=RepoBranchPlan(
                    repo_path=code_repo,
                    source_branch="main",
                    work_branch="ar/fix-thing",
                    base_commit=code_base,
                ),
                memory=RepoBranchPlan(
                    repo_path=memory_repo,
                    source_branch="main",
                    work_branch="ar/fix-thing",
                    base_commit=memory_seed,
                ),
            )
            with mock.patch.object(start_module, "ensure_worktree", return_value="would-create"):
                result: dict[str, Any] = worktree_manager.prepare_memory_for_start(
                    contract, worktree_manager.WorktreeArgs(memory_choice=None, dry_run=True)
                )
            self.assertEqual(result["state"], "compatible")
            self.assertTrue((memory_repo / "onboarding" / "fresh.md").is_file())

    def test_start_reports_compatible_external_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo = root / "repo-a"
            code_base = init_repo(code_repo, "main")
            memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
            memory_base = init_repo(memory_repo, "main")
            write_ledger(
                memory_repo / "memory.md",
                create_initial_ledger("repo-a", code_base, memory_base),
            )
            git(memory_repo, "add", "memory.md")
            git(memory_repo, "commit", "-m", "ledger")
            contract = default_contract(
                ContractTask(
                    name="Fix Thing",
                    repo_name="repo-a",
                    coordination_root=root / "ar-coordination",
                    workflow_kind="light-task",
                    memory_mode="external",
                ),
                leaf=LeafIdentity(worktree_name="fix-thing"),
                code=RepoBranchPlan(
                    repo_path=code_repo,
                    source_branch="main",
                    work_branch="ar/fix-thing",
                    base_commit=code_base,
                ),
                memory=RepoBranchPlan(
                    repo_path=memory_repo,
                    source_branch="main",
                    work_branch="ar/fix-thing",
                    base_commit=memory_base,
                ),
            )
            with mock.patch.object(start_module, "ensure_worktree", return_value="would-create"):
                result: dict[str, Any] = worktree_manager.prepare_memory_for_start(
                    contract, worktree_manager.WorktreeArgs(memory_choice=None, dry_run=True)
                )
            self.assertEqual(result["state"], "compatible")
            self.assertEqual(result["worktree"], "would-create")

    def test_start_reports_internal_memory_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = default_contract(
                ContractTask(
                    name="Fix Thing",
                    repo_name="repo-a",
                    coordination_root=root / "ar-coordination",
                    workflow_kind="light-task",
                    memory_mode="internal",
                ),
                leaf=LeafIdentity(worktree_name="fix-thing"),
                code=RepoBranchPlan(
                    repo_path=root / "repo-a",
                    source_branch="main",
                    work_branch="ar/fix-thing",
                    base_commit="c1",
                ),
            )
            result: dict[str, Any] = worktree_manager.prepare_memory_for_start(
                contract, worktree_manager.WorktreeArgs(memory_choice=None, dry_run=True)
            )
            self.assertEqual(result["state"], "internal")

    def test_missing_mapping_block_advertises_only_consumable_choices(self) -> None:
        # FINDING 7 (260703-L18 / friction F-R): the missing-mapping recovery block must name ONLY
        # executable choices -- passing each advertised memory_choice must DO something, never return
        # the identical block. 'custom' (wired nowhere) is no longer advertised.
        with tempfile.TemporaryDirectory() as tmp:
            contract, _memory_repo, _unmapped, _content = self._unmapped_external_contract(
                Path(tmp)
            )
            blocked: dict[str, Any] = worktree_manager.prepare_memory_for_start(
                contract, worktree_manager.WorktreeArgs(memory_choice=None, dry_run=True)
            )
            self.assertEqual(blocked["state"], "blocked")
            self.assertEqual(blocked["choices"], ["disabled-memory"])
            self.assertNotIn("custom", blocked["choices"])
            for choice in blocked["choices"]:
                consumed: dict[str, Any] = worktree_manager.prepare_memory_for_start(
                    contract, worktree_manager.WorktreeArgs(memory_choice=choice, dry_run=True)
                )
                self.assertNotEqual(
                    consumed["state"], "blocked", f"advertised choice {choice!r} is a dead-end"
                )
            self.assertEqual(
                worktree_manager.prepare_memory_for_start(
                    contract,
                    worktree_manager.WorktreeArgs(memory_choice="disabled-memory", dry_run=True),
                )["state"],
                "disabled",
            )

    def test_retired_reconciliation_choice_does_not_write_the_protected_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract, memory_repo, unmapped, _content = self._unmapped_external_contract(Path(tmp))
            before = git(memory_repo, "rev-parse", "HEAD")
            result: dict[str, Any] = worktree_manager.prepare_memory_for_start(
                contract,
                worktree_manager.WorktreeArgs(memory_choice="reconciliation", dry_run=False),
            )
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["choices"], ["disabled-memory"])
            ledger = load_ledger(memory_repo / "memory.md")
            self.assertIsNone(find_mapping(ledger, unmapped))
            self.assertEqual(git(memory_repo, "rev-parse", "HEAD"), before)

    def test_retired_reconciliation_choice_is_non_mutating_on_another_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract, memory_repo, _unmapped, _content = self._unmapped_external_contract(Path(tmp))
            git(memory_repo, "checkout", "-b", "some-other-branch")
            result = worktree_manager.prepare_memory_for_start(
                contract,
                worktree_manager.WorktreeArgs(memory_choice="reconciliation", dry_run=False),
            )
            self.assertEqual(result["state"], "blocked")
            self.assertNotIn("Ledger sync", git(memory_repo, "log", "-1", "--format=%s"))

    def test_start_reads_the_exact_named_memory_source_ledger_not_ambient_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract, memory_repo, unmapped, content = self._unmapped_external_contract(Path(tmp))
            git(memory_repo, "checkout", "-b", "source-with-map")
            write_ledger(
                memory_repo / "memory.md",
                prepend_mapping(load_ledger(memory_repo / "memory.md"), unmapped, content),
            )
            git(memory_repo, "add", "memory.md")
            git(memory_repo, "commit", "-m", "map exact source")
            source_head = git(memory_repo, "rev-parse", "HEAD")
            git(memory_repo, "checkout", "main")
            exact = replace(
                contract,
                memory_source_branch="source-with-map",
                memory_base_commit=source_head,
            )

            compatible = worktree_manager.prepare_memory_for_start(
                exact, worktree_manager.WorktreeArgs(dry_run=True)
            )
            self.assertEqual(compatible["state"], "compatible")

            git(memory_repo, "branch", "source-without-map", "main")
            write_ledger(
                memory_repo / "memory.md",
                prepend_mapping(load_ledger(memory_repo / "memory.md"), unmapped, content),
            )
            git(memory_repo, "add", "memory.md")
            git(memory_repo, "commit", "-m", "ambient mapping only")
            source_without = replace(
                contract,
                memory_source_branch="source-without-map",
                memory_base_commit=git(memory_repo, "rev-parse", "source-without-map"),
            )

            blocked = worktree_manager.prepare_memory_for_start(
                source_without, worktree_manager.WorktreeArgs(dry_run=True)
            )
            self.assertEqual(blocked["state"], "blocked")
            self.assertEqual(blocked["choices"], ["disabled-memory"])

    def test_worktree_contract_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = default_contract(
                ContractTask(
                    name="Fix Platform Status",
                    repo_name="device-management",
                    coordination_root=root / "ar-coordination",
                    workflow_kind="light-task",
                    memory_mode="external",
                ),
                leaf=LeafIdentity(worktree_name="fix-platform-status"),
                code=RepoBranchPlan(
                    repo_path=root / "device-management",
                    source_branch="dev",
                    work_branch="feature/fix-platform-status",
                    base_commit="abc123",
                ),
                memory=RepoBranchPlan(
                    repo_path=root / "ar-coordination" / "memory-repos" / "ar-device-management",
                    source_branch="dev",
                    work_branch="feature/fix-platform-status",
                    base_commit="def456",
                ),
            )
            write_contract(contract.contract_path, contract)
            loaded = load_contract(contract.contract_path)
            assert loaded.memory_worktree is not None
            self.assertEqual(
                loaded.task_root,
                root / "ar-coordination" / "tasks" / "device-management" / "fix-platform-status",
            )
            self.assertEqual(loaded.task_artifact, loaded.task_root / "task.md")
            self.assertEqual(
                loaded.worktree_group,
                root
                / "ar-coordination"
                / "worktrees"
                / "device-management"
                / "fix-platform-status-ar",
            )
            self.assertEqual(loaded.memory_mode, "external")
            self.assertEqual(loaded.ledger_path, loaded.memory_worktree / "memory.md")
            self.assertEqual(
                task_root_candidates(
                    root / "ar-coordination", "device-management", "Fix Platform Status"
                ),
                [
                    root
                    / "ar-coordination"
                    / "tasks"
                    / "device-management"
                    / "fix-platform-status",
                    root
                    / "ar-coordination"
                    / "tasks"
                    / "device-management"
                    / "fix-platform-status-ar",
                ],
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    worktree_manager.command_status(
                        Namespace(contract_path=contract.contract_path)
                    ),
                    0,
                )
            self.assertEqual(
                json.loads(output.getvalue())["contract_path"], contract.contract_path.as_posix()
            )

    def test_worktree_contract_failed_atomic_replace_preserves_exact_old_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = default_contract(
                ContractTask(
                    name="Atomic Contract",
                    repo_name="device-management",
                    coordination_root=root / "ar-coordination",
                    workflow_kind="light-task",
                    memory_mode="external",
                ),
                leaf=LeafIdentity(worktree_name="atomic-contract"),
                code=RepoBranchPlan(
                    repo_path=root / "device-management",
                    source_branch="dev",
                    work_branch="feature/atomic-contract",
                    base_commit="abc123",
                ),
                memory=RepoBranchPlan(
                    repo_path=root / "ar-coordination" / "memory-repos" / "ar-device-management",
                    source_branch="dev",
                    work_branch="feature/atomic-contract",
                    base_commit="def456",
                ),
            )
            write_contract(contract.contract_path, contract)
            before = contract.contract_path.read_bytes()
            updated = replace(contract, task_name="Atomic Contract Updated")

            with (
                mock.patch.object(
                    atomic_write.os,
                    "replace",
                    side_effect=OSError("deterministic atomic replace failure"),
                ),
                self.assertRaisesRegex(OSError, "atomic replace failure"),
            ):
                write_contract(updated.contract_path, updated)

            self.assertEqual(contract.contract_path.read_bytes(), before)
            self.assertEqual(load_contract(contract.contract_path).task_name, "Atomic Contract")

    def test_closeout_dry_run_without_approval_reports_commit_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = dirty_open_external_contract_fixture(root)
            args = closeout_args(contract, dry_run=True)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_authorized_closeout_mechanics(args), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["state"], "would-closeout")
            self.assertIsNotNone(contract.memory_worktree)
            assert contract.memory_worktree is not None
            self.assertEqual(
                payload["pairIdentity"]["contractPath"],
                contract.contract_path.resolve().as_posix(),
            )
            self.assertEqual(
                payload["pairIdentity"]["codeRoot"],
                contract.code_worktree.resolve().as_posix(),
            )
            self.assertEqual(
                payload["pairIdentity"]["memoryRoot"],
                contract.memory_worktree.resolve().as_posix(),
            )
            self.assertEqual(payload["phase"], "commit-approval-pending")
            self.assertEqual(payload["nextOperation"], "request_commit_approval")
            self.assertEqual(payload["nextTool"], "worktree_closeout_apply")
            self.assertEqual(
                payload["nextArgs"]["contract_path"], contract.contract_path.as_posix()
            )
            self.assertNotIn("next_command", payload)
            self.assertTrue(payload["commit_approval_required"])
            self.assertEqual(payload["proposed_commits"]["code"]["message"], "Add feature")
            self.assertEqual(payload["proposed_commits"]["memory"]["message"], "Document feature")
            self.assertEqual(payload["proposed_commits"]["ledger"]["message"], "Sync ledger")
            self.assertIn(
                "refresh-onboarding-metadata-and-entity-fingerprints", payload["closeout_order"]
            )
            self.assertEqual(payload["changed_code_paths"], {"count": 1, "sample": ["feature.txt"]})
            self.assertEqual(payload["changed_code_paths_committed"], {"count": 0, "sample": []})
            self.assertEqual(payload["onboarding_metadata_refresh"]["missing"], [])
            self.assertEqual(
                payload["onboarding_metadata_refresh"]["required"],
                {"count": 1, "sample": ["feature.txt"]},
            )
            self.assertEqual(
                payload["onboarding_metadata_refresh"]["unonboarded"],
                {"count": 0, "sample": []},
            )
            self.assertEqual(payload["entity_fingerprint_refresh"]["required"], [])
            self.assertTrue(payload["proposed_commits"]["code"]["would_commit"])
            self.assertTrue(
                payload["proposed_commits"]["memory"]["metadata_refresh_after_code_commit"]
            )
            self.assertTrue(
                payload["proposed_commits"]["memory"][
                    "entity_fingerprint_refresh_after_code_commit"
                ]
            )
            self.assertEqual(
                git(contract.code_worktree, "rev-parse", "HEAD"), contract.code_base_commit
            )
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_closeout_plan_uses_memory_worktree_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = dirty_open_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            (contract.memory_worktree / "onboarding" / "feature.txt.md").unlink()
            system_root = contract.memory_worktree / "system"
            system_root.mkdir(parents=True)
            (system_root / "settings.md").write_text("# Settings\n", encoding="utf-8")
            (system_root / "settings.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "onboarding": {
                            "storage": {"mode": "memory-repo"},
                            "pathRules": {
                                "include": {"paths": ["feature.txt"], "fileTypes": [".txt"]},
                                "exclude": {"paths": ["feature.txt"], "fileTypes": []},
                            },
                        },
                        "crossRepo": {"allow": []},
                    }
                ),
                encoding="utf-8",
            )

            plan = worktree_manager.onboarding_refresh_plan(contract, ["feature.txt"])

            self.assertEqual(plan["required"], [])
            self.assertEqual((plan["missing"], plan["unsupported"]), ([], []))

    def test_changed_worktree_paths_includes_long_files(self) -> None:
        with long_path_tempdir() as root:
            repo = root / "repo-a"
            init_repo(repo, "main")
            git(repo, "config", "core.longpaths", "true")
            source_path = long_source_path()
            source_file = repo / source_path
            filesystem.mkdir(source_file.parent, parents=True, exist_ok=True)
            filesystem.write_text(source_file, "value = 1\n", encoding="utf-8")
            self.assertGreater(len(str(source_file)), 260)

            paths = worktree_manager.changed_worktree_paths(repo)

            self.assertIn(source_path, paths)

    def test_committed_changed_paths_intersects_base_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo-a"
            base = init_repo(repo, "main")
            first = commit_file(repo, "a.txt", "a\n", "Add a")
            second = commit_file(repo, "b.txt", "b\n", "Add b")

            self.assertEqual(
                worktree_manager.committed_changed_paths(repo, base, ""), ["a.txt", "b.txt"]
            )
            self.assertEqual(worktree_manager.committed_changed_paths(repo, base, first), ["b.txt"])
            self.assertEqual(worktree_manager.committed_changed_paths(repo, base, second), [])

            git(repo, "rm", "-q", "a.txt")
            git(repo, "commit", "-m", "Delete a")
            self.assertEqual(worktree_manager.committed_changed_paths(repo, base, ""), ["b.txt"])

    def test_onboarding_refresh_plan_detects_long_sidecar_paths(self) -> None:
        with long_path_tempdir() as root:
            source_path = long_source_path()
            onboarding_root = root / "memory" / "onboarding"
            onboarding_file = onboarding_root / f"{source_path}.md"
            filesystem.mkdir(onboarding_file.parent, parents=True, exist_ok=True)
            filesystem.write_text(onboarding_file, "# Long path\n", encoding="utf-8")
            self.assertGreater(len(str(onboarding_file)), 260)
            context = Namespace(
                storage=resolver.StorageSettings(mode="memory-repo", default="memory-repo"),
                code_repository_name="repo-a",
                onboarding_root=onboarding_root,
            )

            plan = worktree_manager.onboarding_refresh_plan_for_context(context, [source_path])

            self.assertEqual(plan["required"][0]["source_path"], source_path)
            self.assertEqual((plan["missing"], plan["unsupported"]), ([], []))
