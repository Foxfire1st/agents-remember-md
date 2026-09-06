"""Real atomic integration journal and exact generic profile fixture."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.certification.frozen_run.models import freeze_certification_run
from agents_remember.certification.repository_profiles.canonical import repository_profile_digest
from agents_remember.certification.repository_profiles.models import (
    ProfileMode,
    RepositoryCertificationProfile,
)
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.worktrees.integration.certification import IntegrationCertificationOwner
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.quality import certification_records, clean_executor, gate
from agents_remember.worktrees.modules.quality.certification_terminal import RecordedGateTerminal
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract
from gate_certification_test_support import (
    _checkout_with_profile,
    _green_outcome_factory,
    _lane_for,
)
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    FixtureRepository,
    install_fixture_profile,
)
from test_worktree_support import git, init_repo


@dataclass(frozen=True)
class IntegrationFixture:
    contract: WorktreeContract
    owner: IntegrationCertificationOwner
    runtime: OperationRuntime


def integration_fixture(
    root: Path,
    profile: FixtureRepository = NODE_FIXTURE,
    *,
    contract_factory: Callable[..., WorktreeContract],
) -> IntegrationFixture:
    repo = root / "repo"
    init_repo(repo)
    (repo / "ar-memory").mkdir()
    path = install_fixture_profile(repo, "agents-remember", profile)
    value = RepositoryCertificationProfile.model_validate_json(path.read_bytes())
    # This profile carries complete original publication bindings in reused catalog rows.
    value = value.model_copy(
        update={
            "publishedArtifacts": tuple(
                item.model_copy(update={"maxBytes": 128 * 1024})
                if item.path == "result.json"
                else item
                for item in value.publishedArtifacts
            )
        }
    )
    value = value.model_copy(update={"profileDigest": repository_profile_digest(value)})
    path.write_text(value.model_dump_json())
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Install exact generic integration profile")
    contract = contract_factory(root, kind="series")
    (root / "agents-remember").symlink_to(repo, target_is_directory=True)
    config = root / "settings.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "coordinationRoot": contract.coordination_root.as_posix(),
                "workspaceRoot": root.as_posix(),
                "repositories": {
                    "agents-remember": {
                        "certificationProfile": AGENTS_REMEMBER_PROFILE_REFERENCE.as_posix(),
                    }
                },
            }
        )
    )
    start_or_observe_operation(
        IntegrateOperationInput(
            configPath=config.as_posix(), contractPath=contract.contract_path.as_posix()
        ),
        contract,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
    runtime = OperationRuntime(store)
    return IntegrationFixture(
        contract, IntegrationCertificationOwner(runtime.start(), store), runtime
    )


@dataclass(frozen=True)
class SelectedCodeFixture:
    target: gate.QualityGateTarget
    prepared: certification_records.PreparedCertificationRun
    terminals: tuple[RecordedGateTerminal, ...]

    def render(self) -> dict[str, object]:
        return gate.render_selected_code_certification(
            self.target,
            self.prepared,
            self.terminals,
            diff_base=git(self.target.code_worktree, "rev-parse", "HEAD"),
            plan=gate.QualityGatePlan(mode=self.prepared.frozen_run.repositoryPlan.mode),
        )


def selected_code_fixture(root: Path, *, mode: ProfileMode = "full") -> SelectedCodeFixture:
    """Issue real original objects and physical publications for explicit renderer tests."""
    code = _checkout_with_profile(root / "code")
    admitted, lane, candidate = _lane_for(code, mode)
    group = root / "enclosure"
    prepared = certification_records._persist_admission(
        certification_records.PreparedCertificationRun(
            group,
            freeze_certification_run(admitted.canonical, lane),
        )
    )
    request = clean_executor.CleanQualityRequest(
        code,
        group,
        "agents-remember",
        AGENTS_REMEMBER_PROFILE_REFERENCE,
        mode,
        git(code, "rev-parse", "HEAD"),
    )
    outcome = _green_outcome_factory(group, lane, candidate)(request)
    manifest = outcome.manifest
    assert manifest is not None
    payload = json.loads(
        published_report_path_from_manifest(
            group / "reports",
            manifest,
            manifest.result_decoder.artifactPath,
        ).read_bytes()
    )
    recorded = certification_records.record_published_generation(prepared, manifest, payload)
    assert recorded.as_payload()["refused"] == []
    return SelectedCodeFixture(
        gate.QualityGateTarget(code, group, "agents-remember", AGENTS_REMEMBER_PROFILE_REFERENCE),
        prepared,
        recorded.terminals,
    )


def structural_quality_references() -> dict[str, object]:
    """Wire-shape fixtures only; physical acceptance is tested by selected_code_fixture."""

    def reference(kind: str, number: int) -> dict[str, object]:
        digest = hashlib.sha256(f"{kind}-{number}".encode()).hexdigest()
        return {"kind": kind, "semanticDigest": digest, "contentSha256": digest, "sizeBytes": 1}

    publication = {"canonicalBytes": "{}", "contentSha256": hashlib.sha256(b"{}").hexdigest()}
    return {
        "frozenRun": reference("frozen-run", 0),
        "terminals": [
            {
                "gate": number,
                "result": reference("result-manifest", number),
                "certificate": reference("certificate", number),
                "publication": publication,
            }
            for number in range(1, 5)
        ],
    }
