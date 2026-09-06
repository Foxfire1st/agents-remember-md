from __future__ import annotations

import io
import json
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.kernel import coordination_context_resolver as resolver
from agents_remember.kernel.coordination_context.models import CoordinationRequest
from agents_remember.kernel.coordination_context_resolver import CoordinationHints
from agents_remember.kernel.memory_ledger import parse_ledger_text
from agents_remember.worktrees import git_worktree_manager as worktree_manager
from agents_remember.worktrees.modules import closeout as closeout_module
from agents_remember.worktrees.modules import integrate as integrate_module
from agents_remember.worktrees.modules.closeout_external import (
    ExternalCloseoutEvidence,
    external_closeout_commits,
)
from agents_remember.worktrees.modules.contract_reader import WorktreeContractReader
from agents_remember.worktrees.modules.models import PATH_SAMPLE_LIMIT, VerifiedChange
from agents_remember.worktrees.worktree_contract import load_contract, write_contract
from closeout_input_test_support import MutationEvidenceRecorder
from test_worktree_support import (
    TEST_CERTIFICATION_PROFILE_REFERENCE,
    WorktreeSupportTests,
    claimed_external_contract_fixture,
    closed_external_contract_fixture,
    closeout_args,
    closeout_publication_facts,
    commit_file,
    commit_memory_ledger,
    committed_range_external_contract_fixture,
    dirty_open_external_contract_fixture,
    drift,
    git,
    integrate_args,
    integrated_external_contract_fixture,
    open_external_contract_fixture,
    read_onboarding_field,
    run_authorized_closeout_mechanics,
    write_entity_catalog,
    write_file_onboarding,
    write_passing_route_review,
)


class WorktreeSupport2(WorktreeSupportTests):
    def test_closeout_requires_approval_note_for_real_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = dirty_open_external_contract_fixture(root)
            args = Namespace(
                contract_path=contract.contract_path,
                approved=True,
                approval_note="",
                code_commit_message="Add feature",
                memory_commit_message="Document feature",
                ledger_commit_message="Sync ledger",
                dry_run=False,
                operation_progress=MutationEvidenceRecorder(),
            )
            with self.assertRaises(RuntimeError):
                closeout_module._closeout_approval_note(
                    worktree_manager.WorktreeArgs.from_namespace(args)
                )
            self.assertTrue(worktree_manager.worktree_dirty(contract.code_worktree))
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_closeout_records_commit_approval_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = dirty_open_external_contract_fixture(root)
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=True,
                approval_note="developer approved commit preview",
                code_commit_message="Add feature",
                memory_commit_message="Document feature",
                ledger_commit_message="Sync ledger",
                dry_run=False,
                operation_progress=MutationEvidenceRecorder(),
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_authorized_closeout_mechanics(args),
                    0,
                )
            loaded = load_contract(contract.contract_path)
            self.assertEqual(loaded.closeout_status, "completed")
            self.assertTrue(loaded.approved_for_commit)
            self.assertEqual(loaded.commit_approval_note, "developer approved commit preview")

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_closeout_refreshes_onboarding_metadata_to_new_code_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = dirty_open_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            onboarding_file = contract.memory_worktree / "onboarding" / "feature.txt.md"
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=True,
                approval_note="developer approved commit preview",
                code_commit_message="Add feature",
                memory_commit_message="Document feature",
                ledger_commit_message="Sync ledger",
                dry_run=False,
                operation_progress=MutationEvidenceRecorder(),
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(args),
                    0,
                )
            payload = json.loads(output.getvalue())
            loaded = load_contract(contract.contract_path)
            self.assertEqual(
                read_onboarding_field(onboarding_file, "lastVerifiedCommitHash"), loaded.code_commit
            )
            self.assertEqual(
                read_onboarding_field(onboarding_file, "lastVerifiedCommitHash"),
                payload["code_commit"],
            )
            ledger = parse_ledger_text(
                (contract.memory_worktree / "memory.md").read_text(encoding="utf-8")
            )
            self.assertEqual(ledger.last_verified_code_commit, payload["code_commit"])
            self.assertEqual(ledger.last_memory_content_commit, payload["memory_content_commit"])
            self.assertIn(
                "feature.txt.md",
                git(
                    contract.memory_worktree,
                    "show",
                    "--name-only",
                    "--format=",
                    payload["memory_content_commit"],
                ),
            )

    def test_closeout_blocks_missing_onboarding_for_changed_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = open_external_contract_fixture(root)
            (contract.code_worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
            write_passing_route_review(contract)
            with self.assertRaisesRegex(
                RuntimeError, "Run the c-05-create-or-update-onboarding-files skill"
            ):
                closeout_module._closeout_attestations(
                    contract,
                    closeout_module.closeout_changed_paths(contract),
                    closeout_module.accepted_closeout_memory_pair(contract).no_impact,
                )
            self.assertTrue(worktree_manager.worktree_dirty(contract.code_worktree))
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_closeout_preview_covers_committed_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = committed_range_external_contract_fixture(root)
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=False,
                approval_note="",
                code_commit_message="Add feature",
                memory_commit_message="Document feature",
                ledger_commit_message="Sync ledger",
                dry_run=True,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_authorized_closeout_mechanics(args), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(
                payload["changed_code_paths"],
                {"count": 2, "sample": ["feature.txt", "raw.txt"]},
            )
            self.assertEqual(
                payload["changed_code_paths_committed"],
                {"count": 2, "sample": ["feature.txt", "raw.txt"]},
            )
            self.assertEqual(payload["onboarding_metadata_refresh"]["missing"], [])
            self.assertEqual(
                payload["onboarding_metadata_refresh"]["unonboarded"],
                {"count": 1, "sample": ["raw.txt"]},
            )
            self.assertEqual(
                payload["sidecar_body_gate"]["stale"],
                {"count": 1, "sample": ["feature.txt"]},
            )
            self.assertFalse(payload["proposed_commits"]["code"]["would_commit"])
            self.assertTrue(payload["proposed_commits"]["memory"]["would_commit"])

    def test_closeout_apply_stamps_committed_range_to_existing_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = committed_range_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            sidecar = contract.memory_worktree / "onboarding" / "feature.txt.md"
            sidecar.write_text(
                sidecar.read_text(encoding="utf-8")
                + "\nDocumented the transported change.\n\n## Update History\n\n"
                + "- 2026-06-12T18:00+02:00 — Reviewed the merged feature change.\n",
                encoding="utf-8",
            )
            write_passing_route_review(contract)
            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=True,
                approval_note="developer approved commit preview",
                code_commit_message="Add feature",
                memory_commit_message="Document feature",
                ledger_commit_message="Sync ledger",
                dry_run=False,
                operation_progress=MutationEvidenceRecorder(),
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(args),
                    0,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code_commit"], code_head)
            self.assertEqual(read_onboarding_field(sidecar, "lastVerifiedCommitHash"), code_head)
            self.assertEqual(
                payload["refreshed_onboarding"], {"count": 1, "sample": ["feature.txt"]}
            )
            self.assertEqual(
                payload["unonboarded_changed_paths"], {"count": 1, "sample": ["raw.txt"]}
            )
            ledger = parse_ledger_text(
                (contract.memory_worktree / "memory.md").read_text(encoding="utf-8")
            )
            self.assertEqual(ledger.last_verified_code_commit, code_head)
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "completed")

    def test_closeout_blocks_stale_sidecar_for_committed_range_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = committed_range_external_contract_fixture(root)
            with self.assertRaisesRegex(RuntimeError, "feature.txt"):
                closeout_module._closeout_attestations(
                    contract,
                    closeout_module.closeout_changed_paths(contract),
                    closeout_module.accepted_closeout_memory_pair(contract).no_impact,
                )
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_second_closeout_does_not_regate_prior_closeout_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = closed_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            commit_file(contract.code_worktree, "second.txt", "second\n", "Add second slice")
            write_file_onboarding(
                contract.memory_worktree / "onboarding",
                contract.repo_name,
                "second.txt",
                contract.code_commit,
            )
            write_passing_route_review(contract)
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=False,
                approval_note="",
                code_commit_message="Add second slice",
                memory_commit_message="Document second slice",
                ledger_commit_message="Sync ledger",
                dry_run=True,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_authorized_closeout_mechanics(args), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["changed_code_paths"], {"count": 1, "sample": ["second.txt"]})
            self.assertEqual(payload["sidecar_body_gate"]["stale"], {"count": 0, "sample": []})

    def test_recloseout_requires_plane_owned_reintegration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = integrated_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            assert contract.memory_repo_path is not None
            (contract.code_worktree / "second.txt").write_text("second\n", encoding="utf-8")
            write_file_onboarding(
                contract.memory_worktree / "onboarding",
                contract.repo_name,
                "second.txt",
                contract.code_commit,
            )
            write_passing_route_review(contract)

            output = io.StringIO()
            with (
                mock.patch.object(closeout_module, "require_ordinary_worktree"),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_authorized_closeout_mechanics(closeout_args(contract, dry_run=True)), 0
                )
            preview = json.loads(output.getvalue())
            self.assertTrue(preview["integration_reopen"]["would_reopen"])

            output = io.StringIO()
            with (
                mock.patch.object(closeout_module, "require_ordinary_worktree"),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_authorized_closeout_mechanics(
                        closeout_args(contract),
                    ),
                    0,
                )
            closeout_payload = json.loads(output.getvalue())
            self.assertTrue(closeout_payload["integration_reopen"]["reopened"])
            reopened = load_contract(contract.contract_path)
            self.assertEqual(reopened.integration_status, "not-started")
            self.assertEqual(reopened.integration_strategy, "")
            self.assertEqual(reopened.integrated_code_commit, "")
            self.assertEqual(reopened.integrated_memory_content_commit, "")
            self.assertEqual(reopened.integrated_ledger_commit, "")
            self.assertEqual(
                worktree_manager.status_payload(reopened)["phase"], "integration-pending"
            )

            output = io.StringIO()
            with (
                mock.patch.object(integrate_module, "require_ordinary_worktree"),
                mock.patch.object(integrate_module, "integration_targets", return_value=()),
                mock.patch.object(integrate_module, "_integration_door_block", return_value=None),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    worktree_manager.command_integrate(integrate_args(reopened, dry_run=True)), 0
                )
            self.assertEqual(json.loads(output.getvalue())["state"], "would-integrate")

            with (
                mock.patch.object(integrate_module, "require_ordinary_worktree"),
                mock.patch.object(integrate_module, "integration_targets"),
                self.assertRaisesRegex(RuntimeError, "plane-owned journaled integration operation"),
            ):
                worktree_manager.command_integrate(integrate_args(reopened))
            self.assertEqual(
                load_contract(contract.contract_path).integration_status, "not-started"
            )

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_noop_recloseout_after_integration_keeps_completed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = integrated_external_contract_fixture(root)
            assert contract.memory_repo_path is not None
            code_source = git(contract.code_repo_path, "rev-parse", contract.code_source_branch)
            memory_source = git(
                contract.memory_repo_path, "rev-parse", contract.memory_source_branch
            )

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(closeout_args(contract, dry_run=True)), 0
                )
            preview = json.loads(output.getvalue())
            self.assertFalse(preview["integration_reopen"]["would_reopen"])

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(
                        closeout_args(contract),
                    ),
                    0,
                )
            closeout_payload = json.loads(output.getvalue())
            self.assertFalse(closeout_payload["integration_reopen"]["reopened"])
            loaded = load_contract(contract.contract_path)
            self.assertEqual(loaded.integration_status, "completed")
            self.assertEqual(loaded.integrated_code_commit, contract.integrated_code_commit)
            self.assertEqual(
                loaded.integrated_memory_content_commit,
                contract.integrated_memory_content_commit,
            )
            self.assertEqual(loaded.integrated_ledger_commit, contract.integrated_ledger_commit)
            self.assertEqual(loaded.code_commit, contract.code_commit)
            self.assertEqual(loaded.memory_content_commit, contract.memory_content_commit)
            self.assertEqual(loaded.ledger_commit, contract.ledger_commit)
            self.assertEqual(
                git(contract.code_repo_path, "rev-parse", contract.code_source_branch),
                code_source,
            )
            self.assertEqual(
                git(contract.memory_repo_path, "rev-parse", contract.memory_source_branch),
                memory_source,
            )

    def test_closeout_excludes_sync_transported_committed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = closed_external_contract_fixture(root)
            commit_file(contract.code_repo_path, "other.txt", "other\n", "Parallel landing")
            new_source = git(contract.code_repo_path, "rev-parse", "HEAD")
            git(
                contract.code_worktree,
                "merge",
                "--no-ff",
                "-m",
                "Sync source",
                contract.code_source_branch,
            )
            synced = replace(contract, code_base_commit=new_source)
            write_contract(synced.contract_path, synced)
            write_passing_route_review(synced)
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=False,
                approval_note="",
                code_commit_message="No-op",
                memory_commit_message="No-op",
                ledger_commit_message="No-op",
                dry_run=True,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_authorized_closeout_mechanics(args), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["changed_code_paths"], {"count": 0, "sample": []})
            self.assertEqual(payload["changed_code_paths_committed"], {"count": 0, "sample": []})

    def test_closeout_preview_bounds_committed_path_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = committed_range_external_contract_fixture(root)
            for index in range(PATH_SAMPLE_LIMIT + 5):
                (contract.code_worktree / f"bulk-{index:02d}.txt").write_text(
                    "x\n", encoding="utf-8"
                )
            git(contract.code_worktree, "add", "-A")
            git(contract.code_worktree, "commit", "-m", "Bulk transport")
            write_passing_route_review(contract)
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=False,
                approval_note="",
                code_commit_message="Bulk",
                memory_commit_message="Bulk",
                ledger_commit_message="Bulk",
                dry_run=True,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_authorized_closeout_mechanics(args), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["changed_code_paths"]["count"], PATH_SAMPLE_LIMIT + 7)
            self.assertEqual(len(payload["changed_code_paths"]["sample"]), PATH_SAMPLE_LIMIT)
            unonboarded = payload["onboarding_metadata_refresh"]["unonboarded"]
            self.assertEqual(unonboarded["count"], PATH_SAMPLE_LIMIT + 6)
            self.assertEqual(len(unonboarded["sample"]), PATH_SAMPLE_LIMIT)

    def test_closeout_blocks_memory_commit_when_memory_quality_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = dirty_open_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            memory_head = git(contract.memory_worktree, "rev-parse", "HEAD")
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=True,
                approval_note="developer approved commit preview",
                code_commit_message="Add feature",
                memory_commit_message="Document feature",
                ledger_commit_message="Sync ledger",
                dry_run=False,
                operation_progress=MutationEvidenceRecorder(),
            )
            facts = closeout_publication_facts(contract, args)
            # This component begins with an actual fixture code commit. No selected
            # operation or code gate is asserted; the external owner must refuse
            # its memory commit when its post-refresh checker reports a finding.
            git(contract.code_worktree, "commit", "-m", "Fixture code for memory refresh")
            code_commit = git(contract.code_worktree, "rev-parse", "HEAD")
            change = VerifiedChange(
                commit=code_commit,
                commit_date=git(contract.code_worktree, "show", "-s", "--format=%cI", code_commit),
                changed_paths=facts.worklist["all"],
                working_paths=facts.worklist["working"],
            )
            failed_quality = {
                "ok": False,
                "findingCount": 1,
                "findings": [
                    {
                        "code": "onboarding_drift_test",
                        "path": "feature.txt.md",
                        "message": "test drift",
                    }
                ],
            }
            with (
                mock.patch(
                    "agents_remember.memory_quality.check.run_memory_quality_check",
                    return_value=failed_quality,
                ),
                self.assertRaisesRegex(RuntimeError, "clean memory_quality_check"),
            ):
                external_closeout_commits(
                    contract,
                    facts.args,
                    facts.effective_input,
                    change,
                    ExternalCloseoutEvidence(
                        memory_quality_before_refresh=facts.quality.memory_quality_before_refresh,
                        coherence_no_impact=facts.quality.coherence_no_impact,
                    ),
                )
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_head)
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_closeout_advances_the_stamp_past_a_changed_claim(self) -> None:
        """The writer component stamps and checks a changed claim against its new commit.

        This isolates the existing Git/metadata/claim mechanics. It does not establish
        selected-operation admission or Gate-5 acceptance before code publication.
        """
        with tempfile.TemporaryDirectory() as tmp:
            contract, baseline, sidecar = claimed_external_contract_fixture(Path(tmp))
            (contract.code_worktree / "feature.py").write_text(
                "def stable(value: int) -> int:\n    return value + 2\n",
                encoding="utf-8",
            )
            write_passing_route_review(contract)

            self.assertEqual(
                run_authorized_closeout_mechanics(
                    closeout_args(contract),
                ),
                0,
            )

            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            self.assertNotEqual(code_head, baseline)
            self.assertEqual(read_onboarding_field(sidecar, "lastVerifiedCommitHash"), code_head)
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "completed")

    def test_closeout_refuses_a_deleted_construct_before_the_code_commit(self) -> None:
        """The citation gate snaps first: a deleted anchor rejects before any commit is spent."""
        with tempfile.TemporaryDirectory() as tmp:
            contract, baseline, _sidecar = claimed_external_contract_fixture(Path(tmp))
            assert contract.memory_worktree is not None
            memory_head = git(contract.memory_worktree, "rev-parse", "HEAD")
            (contract.code_worktree / "feature.py").write_text(
                "def renamed(value: int) -> int:\n    return value + 2\n",
                encoding="utf-8",
            )
            write_passing_route_review(contract)

            with self.assertRaisesRegex(RuntimeError, "citation_anchor_absent_from_range"):
                closeout_module._memory_quality_before_refresh(contract)

            self.assertEqual(git(contract.code_worktree, "rev-parse", "HEAD"), baseline)
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_head)
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_closeout_advances_the_stamp_after_a_clean_claim_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract, baseline, sidecar = claimed_external_contract_fixture(Path(tmp))
            (contract.code_worktree / "feature.py").write_text(
                "def stable(value: int) -> int:\n"
                "    return value + 1\n\n"
                "def unrelated() -> int:\n"
                "    return 2\n",
                encoding="utf-8",
            )
            write_passing_route_review(contract)
            output = io.StringIO()

            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(
                        closeout_args(contract),
                    ),
                    0,
                )

            payload = json.loads(output.getvalue())
            self.assertNotEqual(payload["code_commit"], baseline)
            self.assertEqual(
                read_onboarding_field(sidecar, "lastVerifiedCommitHash"),
                payload["code_commit"],
            )
            self.assertEqual(
                payload["memory_quality"]["closeoutPhases"]["beforeMetadataRefresh"],
                [
                    "style.document_shape.entity_catalog_alignment",
                    "style.citations.range_resolution",
                    "style.citations.claim_reopen",
                ],
            )
            self.assertIn(
                "style.citations.claim_reopen",
                payload["memory_quality"]["closeoutPhases"]["afterMetadataRefresh"],
            )
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "completed")

    def test_closeout_refreshes_entity_fingerprint_after_code_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = open_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            (contract.code_worktree / "feature.txt").write_text("old\n", encoding="utf-8")
            git(contract.code_worktree, "add", "feature.txt")
            git(contract.code_worktree, "commit", "-m", "Add feature baseline")
            baseline_commit = git(contract.code_worktree, "rev-parse", "HEAD")
            write_file_onboarding(
                contract.memory_worktree / "onboarding",
                contract.repo_name,
                "feature.txt",
                baseline_commit,
            )
            seed_fingerprint = drift.compute_git_blob_set_fingerprint(
                contract.code_worktree, ["feature.txt"]
            )
            catalog = write_entity_catalog(
                contract.memory_worktree / "onboarding",
                contract.repo_name,
                [("Feature", drift.GIT_BLOB_SET_ALGORITHM, seed_fingerprint, ["feature.txt"])],
            )
            (contract.code_worktree / "feature.txt").write_text("new\n", encoding="utf-8")
            write_passing_route_review(contract)
            args = Namespace(
                contract_path=contract.contract_path,
                certification_profile=TEST_CERTIFICATION_PROFILE_REFERENCE,
                approved=True,
                approval_note="developer approved commit preview",
                code_commit_message="Update feature",
                memory_commit_message="Document feature update",
                ledger_commit_message="Sync ledger",
                dry_run=False,
                operation_progress=MutationEvidenceRecorder(),
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(args),
                    0,
                )
            payload = json.loads(output.getvalue())
            expected = drift.compute_git_blob_set_fingerprint(
                contract.code_worktree, ["feature.txt"]
            )
            self.assertNotEqual(expected, seed_fingerprint)
            self.assertEqual(payload["refreshed_entities"][0]["entity"], "Feature")
            self.assertIn(expected, catalog.read_text(encoding="utf-8"))
            self.assertIn(
                "entities.md",
                git(
                    contract.memory_worktree,
                    "show",
                    "--name-only",
                    "--format=",
                    payload["memory_content_commit"],
                ),
            )

    def test_status_reports_integration_pending_for_dirty_closed_contract(self) -> None:
        # slice 09: a dirty tree is no longer read as a commit-approval gate. A closed-out contract reports
        # its honest lifecycle position (integration-pending) even when the worktree is dirty;
        # commit-approval-pending is owned by the closeout preview / a raised gate, not `git status`.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = closed_external_contract_fixture(root)
            (contract.code_worktree / "followup.txt").write_text("follow-up\n", encoding="utf-8")
            payload = worktree_manager.status_payload(contract)
            self.assertEqual(payload["phase"], "integration-pending")
            self.assertEqual(payload["nextOperation"], "request_integration_decision")
            self.assertEqual(payload.get("nextTool"), "worktree_integrate")
            self.assertNotIn("next_command", payload)

    def test_direct_integrate_cannot_fast_forward_code_or_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = closed_external_contract_fixture(root)
            assert contract.memory_worktree is not None
            assert contract.memory_repo_path is not None
            args = Namespace(
                contract_path=contract.contract_path,
                approved=True,
                strategy="ff-only",
                ledger_commit_message="",
                dry_run=False,
            )
            code_source = git(contract.code_repo_path, "rev-parse", contract.code_source_branch)
            memory_source = git(
                contract.memory_repo_path, "rev-parse", contract.memory_source_branch
            )
            with (
                mock.patch.object(integrate_module, "require_ordinary_worktree"),
                mock.patch.object(integrate_module, "integration_targets"),
                self.assertRaisesRegex(RuntimeError, "plane-owned journaled integration operation"),
            ):
                worktree_manager.command_integrate(args)
            self.assertEqual(
                git(contract.code_repo_path, "rev-parse", contract.code_source_branch),
                code_source,
            )
            self.assertEqual(
                git(contract.memory_repo_path, "rev-parse", contract.memory_source_branch),
                memory_source,
            )
            loaded = load_contract(contract.contract_path)
            self.assertEqual(loaded.integration_status, "not-started")
            self.assertEqual(
                worktree_manager.status_payload(loaded)["phase"], "integration-pending"
            )

    def test_cleanup_blocks_before_integration_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = closed_external_contract_fixture(root)
            args = Namespace(contract_path=contract.contract_path, approved=True, dry_run=False)
            with self.assertRaises(RuntimeError):
                worktree_manager.command_cleanup(args)

    def test_direct_integrate_cannot_classify_parallel_non_overlapping_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = closed_external_contract_fixture(root)
            assert contract.memory_repo_path is not None
            parallel_code = commit_file(
                contract.code_repo_path, "parallel.txt", "parallel\n", "Parallel code change"
            )
            parallel_memory_content = commit_file(
                contract.memory_repo_path,
                "onboarding/parallel.txt.md",
                "# parallel\n",
                "Document parallel change",
            )
            parallel_ledger = commit_memory_ledger(
                contract.memory_repo_path,
                parallel_code,
                parallel_memory_content,
                "Sync parallel ledger",
            )
            args = Namespace(
                contract_path=contract.contract_path,
                approved=True,
                strategy="replay",
                ledger_commit_message="Replay integration ledger",
                dry_run=False,
            )
            with (
                mock.patch.object(integrate_module, "require_ordinary_worktree"),
                mock.patch.object(integrate_module, "integration_targets"),
                self.assertRaisesRegex(RuntimeError, "plane-owned journaled integration operation"),
            ):
                worktree_manager.command_integrate(args)
            loaded = load_contract(contract.contract_path)
            self.assertEqual(loaded.integration_status, "not-started")
            self.assertEqual(
                git(contract.code_repo_path, "rev-parse", contract.code_source_branch),
                parallel_code,
            )
            self.assertEqual(
                git(contract.memory_repo_path, "rev-parse", contract.memory_source_branch),
                parallel_ledger,
            )
            self.assertTrue((contract.code_repo_path / "parallel.txt").exists())

    def test_direct_integrate_cannot_classify_parallel_conflicting_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = closed_external_contract_fixture(
                root, code_path="README.md", code_content="# Task\n"
            )
            assert contract.memory_repo_path is not None
            parallel_code = commit_file(
                contract.code_repo_path, "README.md", "# Parallel\n", "Parallel conflicting change"
            )
            args = Namespace(
                contract_path=contract.contract_path,
                approved=True,
                strategy="replay",
                ledger_commit_message="Replay integration ledger",
                dry_run=False,
            )
            with (
                mock.patch.object(integrate_module, "require_ordinary_worktree"),
                mock.patch.object(integrate_module, "integration_targets"),
                self.assertRaisesRegex(RuntimeError, "plane-owned journaled integration operation"),
            ):
                worktree_manager.command_integrate(args)
            self.assertEqual(
                git(contract.code_repo_path, "rev-parse", contract.code_source_branch),
                parallel_code,
            )
            self.assertEqual(
                git(contract.memory_repo_path, "rev-parse", contract.memory_source_branch),
                contract.memory_base_commit,
            )
            loaded = load_contract(contract.contract_path)
            self.assertEqual(loaded.integration_status, "not-started")
            self.assertEqual(
                worktree_manager.status_payload(loaded)["phase"], "integration-pending"
            )

    def test_resolver_explicit_internal_uses_existing_ar_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "my-app"
            repo.mkdir()
            (repo / "ar-memory").mkdir()
            context = resolver.resolve_coordination_context(
                code_repository_name="my-app",
                workspace_root=workspace,
                request=CoordinationRequest(
                    hints=CoordinationHints(topology="internal"),
                    contract_reader=WorktreeContractReader(),
                ),
            )
            self.assertEqual(context.coordination_root, repo / "ar-coordination")
            self.assertEqual(context.memory_root, repo / "ar-memory")
            self.assertEqual(context.onboarding_root, repo / "ar-memory" / "onboarding")
            self.assertEqual(context.temp_root, repo / "ar-coordination" / "temp")

    def test_resolver_prefers_existing_internal_memory_over_external_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "repo-a"
            repo.mkdir()
            (repo / "ar-memory").mkdir()
            external_memory = workspace / "ar-coordination" / "memory-repos" / "ar-repo-a"
            external_memory.mkdir(parents=True)
            context = resolver.resolve_coordination_context(
                code_repository_name="repo-a",
                workspace_root=workspace,
                request=CoordinationRequest(
                    hints=CoordinationHints(coordination_root=workspace / "ar-coordination"),
                    contract_reader=WorktreeContractReader(),
                ),
            )
            self.assertEqual(context.topology, "internal")
            self.assertEqual(context.memory_root, repo / "ar-memory")
            self.assertEqual(context.coordination_root, repo / "ar-coordination")

    def test_resolver_uses_external_memory_repo_when_internal_memory_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "repo-a"
            repo.mkdir()
            external_memory = workspace / "ar-coordination" / "memory-repos" / "ar-repo-a"
            external_memory.mkdir(parents=True)
            context = resolver.resolve_coordination_context(
                code_repository_name="repo-a",
                workspace_root=workspace,
                request=CoordinationRequest(
                    hints=CoordinationHints(coordination_root=workspace / "ar-coordination"),
                    contract_reader=WorktreeContractReader(),
                ),
            )
            self.assertEqual(context.topology, "external")
            self.assertEqual(context.coordination_root, workspace / "ar-coordination")
            self.assertEqual(context.memory_root, external_memory)
            self.assertEqual(context.onboarding_root, external_memory / "onboarding")

    def test_resolver_ignores_dot_env_override_for_coordination_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "repo-a"
            repo.mkdir()
            agents_repo = workspace / "agents-remember"
            agents_repo.mkdir()
            (agents_repo / ".env").write_text(
                "AR_COORDINATION_ROOT=custom-coordination\n", encoding="utf-8"
            )
            (agents_repo / "custom-coordination" / "memory-repos" / "ar-repo-a").mkdir(parents=True)
            external_memory = workspace / "ar-coordination" / "memory-repos" / "ar-repo-a"
            external_memory.mkdir(parents=True)
            with mock.patch.object(resolver, "agents_repo_from_script", return_value=agents_repo):
                context = resolver.resolve_coordination_context(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    request=CoordinationRequest(contract_reader=WorktreeContractReader()),
                )
            self.assertEqual(context.topology, "external")
            self.assertEqual(context.coordination_root, workspace / "ar-coordination")
            self.assertEqual(context.memory_root, external_memory)

    def test_resolver_uses_installed_runtime_root_as_coordination_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "repo-a"
            repo.mkdir()
            coordination_root = workspace / "runtime-home"
            (coordination_root / "skills").mkdir(parents=True)
            (coordination_root / "system").mkdir()
            (coordination_root / "tasks").mkdir()
            external_memory = coordination_root / "memory-repos" / "ar-repo-a"
            external_memory.mkdir(parents=True)
            with mock.patch.object(
                resolver, "agents_repo_from_script", return_value=coordination_root
            ):
                context = resolver.resolve_coordination_context(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    request=CoordinationRequest(contract_reader=WorktreeContractReader()),
                )
            self.assertEqual(context.topology, "external")
            self.assertEqual(context.coordination_root, coordination_root)
            self.assertEqual(context.memory_root, external_memory)

    def test_resolver_ignores_dot_env_example_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "repo-a"
            repo.mkdir()
            agents_repo = workspace / "agents-remember"
            agents_repo.mkdir()
            (agents_repo / ".env.example").write_text(
                "AR_COORDINATION_ROOT=example-coordination\n", encoding="utf-8"
            )
            (agents_repo / "example-coordination" / "memory-repos" / "ar-repo-a").mkdir(
                parents=True
            )
            with (
                mock.patch.object(resolver, "agents_repo_from_script", return_value=agents_repo),
                self.assertRaises(resolver.MissingMemoryError) as raised,
            ):
                resolver.resolve_coordination_context(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    request=CoordinationRequest(contract_reader=WorktreeContractReader()),
                )
            self.assertEqual(raised.exception.coordination_root, workspace / "ar-coordination")

    def test_resolver_does_not_select_legacy_external_onboarding_without_memory_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "repo-a"
            repo.mkdir()
            coordination_root = workspace / "ar-coordination"
            (coordination_root / "onboarding" / "repo-a").mkdir(parents=True)
            with self.assertRaises(resolver.MissingMemoryError):
                resolver.resolve_coordination_context(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    request=CoordinationRequest(
                        hints=CoordinationHints(coordination_root=coordination_root),
                        contract_reader=WorktreeContractReader(),
                    ),
                )
