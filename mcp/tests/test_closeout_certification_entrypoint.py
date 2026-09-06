"""Public admission and actual worker execution of selected certification authorities.

Only process launch, the Dagger subprocess boundary, and downstream continuation are
injected. Linked Git repositories, profile admission, immutable publication, terminal
selection, and OperationRuntime execute their production owners.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agents_remember.certification.repository_profiles.canonical import repository_profile_digest
from agents_remember.certification.repository_profiles.models import (
    RepositoryCertificationProfile,
)
from agents_remember.models.test_evidence import _certifying_evidence_from_verified_dagger
from agents_remember.tasks import write_task_doc
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.quality import clean_executor
from agents_remember.worktrees.modules.quality.execution import sandbox
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
    published_manifest_payload,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract
from gate_certification_test_support import _gate_catalog
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    FixtureRepository,
    install_fixture_profile,
)
from test_closeout_queue import MASTER_A, QueueFixture, _leaf
from test_worktree_support import git


def _fixture(
    root: Path,
    profile: FixtureRepository = NODE_FIXTURE,
    *,
    profile_reference: Path = AGENTS_REMEMBER_PROFILE_REFERENCE,
    candidate_file: tuple[str, str] | None = None,
) -> QueueFixture:
    fixture = QueueFixture(root, memory_mode="internal")
    contract = fixture.contracts[MASTER_A]
    installed = install_fixture_profile(contract.code_worktree, contract.repo_name, profile)
    declared = RepositoryCertificationProfile.model_validate_json(installed.read_bytes())
    # Reused results retain up to three complete original publication manifests;
    # measured suffix catalogs reach 12,795 bytes rather than the fresh fixture's 4 KiB.
    declared = declared.model_copy(
        update={
            "publishedArtifacts": tuple(
                item.model_copy(update={"maxBytes": 16 * 1024})
                if item.path == "result.json"
                else item
                for item in declared.publishedArtifacts
            )
        }
    )
    declared = declared.model_copy(update={"profileDigest": repository_profile_digest(declared)})
    installed.write_text(declared.model_dump_json())
    if profile_reference != AGENTS_REMEMBER_PROFILE_REFERENCE:
        configured = contract.code_worktree / profile_reference
        configured.parent.mkdir(parents=True, exist_ok=True)
        installed.rename(configured)
        settings = json.loads(fixture.config_path.read_text())
        settings["repositories"][contract.repo_name]["certificationProfile"] = (
            profile_reference.as_posix()
        )
        fixture.config_path.write_text(json.dumps(settings))
    git(contract.code_worktree, "add", "-A")
    if candidate_file is not None:
        name, contents = candidate_file
        (contract.code_worktree / name).write_text(contents, encoding="utf-8")
    _review_and_declare(fixture)
    return fixture


def _review_and_declare(fixture: QueueFixture) -> None:
    """Bind the real route-review owner and door to this fixture's changed candidate."""
    contract = fixture.contracts[MASTER_A]
    slug = Path(fixture.leaf_refs[MASTER_A].path).stem
    write_task_doc(contract.task_root, _leaf(contract, slug))
    fixture.declare(MASTER_A)


def _store(contract: WorktreeContract) -> LifecycleOperationStore:
    return LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))


def _export_record(root: Path, relative: str) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"isolated executor fixture: {relative}\n".encode()
    path.write_bytes(payload)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def _mark_gate_red(catalog: dict[str, object]) -> None:
    rails = catalog["rails"]
    assert isinstance(rails, list)
    for rail in rails:
        if rail["status"] == "pass":
            rail.update(status="fail", exitCode=1)
    catalog["disposition"] = "red"


def _executor(
    profile: FixtureRepository,
    calls: list[clean_executor.CleanQualityRequest],
    *,
    fail_gate: int | None = None,
    interrupt_gate: int | None = None,
    transform_terminal: Callable[[dict[str, Any]], None] | None = None,
):
    def execute(request: clean_executor.CleanQualityRequest) -> clean_executor.CleanQualityOutcome:
        assert request.execution is not None
        request.execution.validate()
        prepared = clean_executor._prepare_sandbox(request)
        admitted_execution = sandbox._admit_prepared_profile(request, prepared)
        assert request.authorize_start is not None
        request.authorize_start()
        calls.append(request)
        run = request.execution.run
        exported = request.worktree_group / "fixture-export"
        artifact_paths = {
            profile.suite_artifact: profile.suite_publication,
            profile.coverage_artifact: profile.coverage_publication,
        }
        for declared in run.repositoryProfile.profile.publishedArtifacts:
            _export_record(exported, declared.path)
        catalogs = []
        for catalog in _gate_catalog(run, exported, supplemental_artifact_paths=artifact_paths):
            planned_gate = catalog["gate"]
            assert isinstance(planned_gate, int)
            if planned_gate < request.execution.first_gate:
                retained = request.execution.retained[planned_gate - 1]
                catalogs.append(
                    {
                        "gate": planned_gate,
                        "disposition": "reused",
                        "started": False,
                        "zeroStart": True,
                        "rails": [],
                        "certificateDigest": retained.certificate.certificateDigest,
                        "resultManifestDigest": retained.result.manifestDigest,
                        "originalPublication": published_manifest_payload(retained.publication),
                    }
                )
                continue
            if fail_gate is not None and planned_gate > fail_gate:
                break
            if interrupt_gate is not None and planned_gate > interrupt_gate:
                break
            if planned_gate == fail_gate:
                _mark_gate_red(catalog)
            if planned_gate == interrupt_gate:
                catalog["disposition"] = "interrupted"
            catalogs.append(catalog)
        exit_code = 1 if fail_gate is not None or interrupt_gate is not None else 0
        decoder = admitted_execution.decoder
        terminal = {
            decoder.statusField: decoder.failedValue if exit_code else decoder.passedValue,
            decoder.exitCodeField: exit_code,
            "gates": catalogs,
        }
        if transform_terminal is not None:
            transform_terminal(terminal)
        (exported / decoder.artifactPath).write_text(json.dumps(terminal))
        assert admitted_execution.admitted.canonical == run.repositoryProfile
        clean_executor._publish_reports(
            exported,
            request.worktree_group / "reports",
            candidate_tree=run.repositoryPlan.candidateIdentity.value,
            profile_execution=admitted_execution,
            bindings=clean_executor.ReportBindings(
                attestation=request.attestation,
                runtime_authority_digest=None,
                protected_generations=request.protected_generations,
            ),
        )
        manifest = load_published_quality_manifest(request.worktree_group / "reports")
        evidence = _certifying_evidence_from_verified_dagger(
            candidate_tree=manifest.candidate_tree,
            result_sha256=manifest.require_file(manifest.result_decoder.artifactPath).sha256,
        )
        return clean_executor.CleanQualityOutcome(
            subprocess.CompletedProcess(
                ["isolated-executor-adapter"], exit_code, stdout=f"fixture exit {exit_code}\n"
            ),
            evidence,
            manifest,
        )

    return execute
