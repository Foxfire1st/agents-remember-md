"""One typed builder for repository-profile contract tests and fixture repositories."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
    RailClass,
    RailEvidenceContract,
    RailIdentity,
    RailRuntimeInputs,
)
from agents_remember.certification.repository_profiles.authority import (
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.canonical import (
    canonicalize_repository_profile,
    repository_profile_digest,
)
from agents_remember.certification.repository_profiles.execution import (
    AdmittedRepositoryProfileExecution,
    admit_repository_profile_execution,
)
from agents_remember.certification.repository_profiles.models import (
    DaggerModuleExecutorDefinition,
    JsonExitStatusDecoderDefinition,
    ProfileMode,
    ProfilePurpose,
    PublishedArtifactDefinition,
    RepositoryCertificationProfile,
    RepositoryGateId,
    RepositoryGateSelection,
    RepositoryProfileSelection,
    RepositoryRailDefinition,
    RepositoryRailExecution,
    RepositorySelectorAuthority,
)
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENTS_REMEMBER_PROFILE_REFERENCE = Path("mcp/certification-profile-v1.json")
PROFILE_FIXTURE_ROOT = REPOSITORY_ROOT / "mcp/tests/fixtures/repository_profiles"

_NODE_IMAGE = (
    "mcr.microsoft.com/playwright:v1.60.0-noble@"
    "sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9"
)
_RUST_IMAGE = (
    "rust:1.85.0-bookworm@sha256:0ff31c9ffa641a62e48d543fb00b4960955ea375f40776f40f585b89e654cc5e"
)


def install_agents_remember_profile(repository_root: Path) -> Path:
    """Copy the ordinary repository-owned profile into a throwaway Git fixture."""

    destination = repository_root / AGENTS_REMEMBER_PROFILE_REFERENCE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPOSITORY_ROOT / AGENTS_REMEMBER_PROFILE_REFERENCE, destination)
    return destination


def install_fixture_profile(
    repository_root: Path,
    repository_id: str,
    fixture: FixtureRepository | None = None,
) -> Path:
    """Install one repository-matched generic profile in a temporary fixture."""

    profile = fixture_profile(fixture or NODE_FIXTURE).model_copy(
        update={
            "repositoryId": repository_id,
            "profileId": f"{repository_id}-certification",
            "profileDigest": "0" * 64,
        }
    )
    profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
    destination = repository_root / AGENTS_REMEMBER_PROFILE_REFERENCE
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def agents_remember_profile_execution(
    *,
    candidate_tree: str,
    mode: ProfileMode = "full",
    purpose: ProfilePurpose = "closeout",
    repository_root: Path = REPOSITORY_ROOT,
) -> AdmittedRepositoryProfileExecution:
    """Admit the checked-in reference profile for publication/executor tests."""

    admitted = load_repository_profile(
        "agents-remember",
        repository_root,
        AGENTS_REMEMBER_PROFILE_REFERENCE,
    )
    return admit_repository_profile_execution(
        admitted,
        purpose=purpose,
        mode=mode,
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
    )


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dagger_runtime_digest() -> str:
    """Match the repository Dagger adapter's canonical source digest."""

    source_root = REPOSITORY_ROOT / ".dagger/src/agents_remember_quality"
    sources = {
        path.relative_to(source_root).as_posix(): _file_digest(path)
        for path in sorted(source_root.glob("*.py"))
    }
    return content_digest(sources)


@dataclass(frozen=True)
class FixtureRepository:
    repository_id: str
    language: str
    source_root: Path
    source_files: tuple[Path, ...]
    runtime_identity: str
    image_reference: str
    runtime_manifests: tuple[Path, ...]
    gate_one_command: tuple[str, ...]
    suite_command: tuple[str, ...]
    post_command: tuple[str, ...]
    e2e_command: tuple[str, ...]
    suite_artifact: str
    coverage_artifact: str
    suite_publication: str
    coverage_publication: str
    e2e_publication: str
    dagger_function: str


@dataclass(frozen=True)
class _RailSpec:
    rail_id: str
    gate: RepositoryGateId
    rail_class: RailClass
    command: tuple[str, ...]
    prerequisites: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    output_artifacts: tuple[str, ...] = ()
    selector: str | None = None
    clean_room: bool = False


NODE_FIXTURE = FixtureRepository(
    repository_id="fixture-node",
    language="typescript",
    source_root=PROFILE_FIXTURE_ROOT / "node",
    source_files=tuple(
        Path(path)
        for path in (
            "mcp/tests/fixtures/repository_profiles/node/package-lock.json",
            "mcp/tests/fixtures/repository_profiles/node/package.json",
            "mcp/tests/fixtures/repository_profiles/node/scripts/coverage-check.mjs",
            "mcp/tests/fixtures/repository_profiles/node/scripts/lint.mjs",
            "mcp/tests/fixtures/repository_profiles/node/scripts/run-e2e.mjs",
            "mcp/tests/fixtures/repository_profiles/node/scripts/run-suite.mjs",
            "mcp/tests/fixtures/repository_profiles/node/scripts/select-tests.sh",
            "mcp/tests/fixtures/repository_profiles/node/src/math.mjs",
            "mcp/tests/fixtures/repository_profiles/node/test/e2e.test.mjs",
            "mcp/tests/fixtures/repository_profiles/node/test/unit.test.mjs",
        )
    ),
    runtime_identity="node-playwright-v1.60.0-noble",
    image_reference=_NODE_IMAGE,
    runtime_manifests=(
        Path("mcp/tests/fixtures/repository_profiles/node/package.json"),
        Path("mcp/tests/fixtures/repository_profiles/node/package-lock.json"),
    ),
    gate_one_command=("node", "scripts/lint.mjs"),
    suite_command=(
        "node",
        "scripts/run-suite.mjs",
        "{reports}/node-suite.json",
        "{reports}/node-coverage.json",
        "{selected-tests}",
    ),
    post_command=(
        "node",
        "scripts/coverage-check.mjs",
        "{reports}/node-coverage.json",
    ),
    e2e_command=(
        "node",
        "scripts/run-e2e.mjs",
        "{reports}/node-e2e.json",
    ),
    suite_artifact="node-suite-result",
    coverage_artifact="node-coverage",
    suite_publication="node-suite.json",
    coverage_publication="node-coverage.json",
    e2e_publication="node-e2e.json",
    dagger_function="portable-certification",
)

RUST_FIXTURE = FixtureRepository(
    repository_id="fixture-rust",
    language="rust",
    source_root=PROFILE_FIXTURE_ROOT / "rust",
    source_files=tuple(
        Path(path)
        for path in (
            "mcp/tests/fixtures/repository_profiles/rust/Cargo.lock",
            "mcp/tests/fixtures/repository_profiles/rust/Cargo.toml",
            "mcp/tests/fixtures/repository_profiles/rust/scripts/post-suite.sh",
            "mcp/tests/fixtures/repository_profiles/rust/scripts/run-e2e.sh",
            "mcp/tests/fixtures/repository_profiles/rust/scripts/run-suite.sh",
            "mcp/tests/fixtures/repository_profiles/rust/scripts/select-tests.sh",
            "mcp/tests/fixtures/repository_profiles/rust/src/lib.rs",
            "mcp/tests/fixtures/repository_profiles/rust/tests/service.rs",
            "mcp/tests/fixtures/repository_profiles/rust/tests/unit.rs",
        )
    ),
    runtime_identity="rust-1.85.0-bookworm",
    image_reference=_RUST_IMAGE,
    runtime_manifests=(
        Path("mcp/tests/fixtures/repository_profiles/rust/Cargo.toml"),
        Path("mcp/tests/fixtures/repository_profiles/rust/Cargo.lock"),
    ),
    gate_one_command=("cargo", "check", "--locked"),
    suite_command=(
        "sh",
        "scripts/run-suite.sh",
        "{reports}/rust-suite.json",
        "{reports}/rust-suite-proof.json",
        "{selected-tests}",
    ),
    post_command=(
        "sh",
        "scripts/post-suite.sh",
        "{reports}/rust-suite-proof.json",
    ),
    e2e_command=(
        "sh",
        "scripts/run-e2e.sh",
        "{reports}/rust-e2e.json",
    ),
    suite_artifact="rust-suite-result",
    coverage_artifact="rust-suite-proof",
    suite_publication="rust-suite.json",
    coverage_publication="rust-suite-proof.json",
    e2e_publication="rust-e2e.json",
    dagger_function="portable-certification",
)


def fixture_profile(fixture: FixtureRepository = NODE_FIXTURE) -> RepositoryCertificationProfile:
    _require_complete_fixture_source(fixture)
    rails = (
        _rail(
            fixture,
            _RailSpec(
                "static-quality",
                gate=1,
                rail_class="pre-test-quality",
                command=fixture.gate_one_command,
            ),
        ),
        _rail(
            fixture,
            _RailSpec(
                "ordinary-suite",
                gate=2,
                rail_class="ordinary-test-suite",
                command=fixture.suite_command,
                prerequisites=("static-quality",),
                output_artifacts=(fixture.suite_artifact, fixture.coverage_artifact),
                selector="repository-test-selector",
            ),
        ),
        _rail(
            fixture,
            _RailSpec(
                "post-suite-quality",
                gate=3,
                rail_class="post-test-quality",
                command=fixture.post_command,
                prerequisites=("ordinary-suite",),
                required_artifacts=(fixture.coverage_artifact,),
            ),
        ),
        _rail(
            fixture,
            _RailSpec(
                "clean-room-e2e",
                gate=4,
                rail_class="integration-test",
                command=fixture.e2e_command,
                prerequisites=("post-suite-quality",),
                clean_room=True,
            ),
        ),
    )
    rail_ids = tuple(rail.identity for rail in rails)
    selections = tuple(
        _selection(selection_id, purpose, mode, rail_ids)
        for selection_id, purpose, mode in (
            ("local-targeted", "local-precommit", "targeted"),
            ("closeout-targeted", "closeout", "targeted"),
            ("closeout-full", "closeout", "full"),
        )
    )
    profile = RepositoryCertificationProfile(
        semanticRevision="1.0.0",
        repositoryId=fixture.repository_id,
        profileId=f"{fixture.repository_id}-certification",
        profileDigest="0" * 64,
        selections=selections,
        rails=rails,
        selectors=(
            RepositorySelectorAuthority(
                selectorId="repository-test-selector",
                schemaVersion="repository-selector-result/v1",
                version="1.0.0",
                configurationDigest=_file_digest(fixture.source_root / "scripts/select-tests.sh"),
                inputUniverse=(f"{fixture.language} tracked inputs",),
                outputArtifacts=("selected-tests",),
                workingDirectory=".",
                command=("sh", "scripts/select-tests.sh", "{selector-output}"),
                resultPath=".certification/selected-tests.json",
            ),
        ),
        executorAdapters=(
            DaggerModuleExecutorDefinition(
                adapterId="certifying-dagger",
                version="0.21.8",
                executable="dagger",
                functionName=fixture.dagger_function,
                sourceArgument="source",
                repositoryBundleArgument="repository-bundle",
                diffBaseArgument="diff-base",
                memoryCapArgument="memory-cap-bytes",
                planArgument="execution-manifest",
                reportsField="reports",
                imageReference=fixture.image_reference,
                runtimeDigest=dagger_runtime_digest(),
                consumingGates=(1, 2, 3, 4),
            ),
        ),
        resultDecoders=(
            JsonExitStatusDecoderDefinition(
                decoderId="terminal-result",
                artifactPath="result.json",
                statusField="status",
                exitCodeField="exit-code",
                passedValue="passed",
                failedValue="failed",
                consumingGates=(1, 2, 3, 4),
            ),
        ),
        publishedArtifacts=(
            PublishedArtifactDefinition(
                path="result.json",
                mediaType="application/json",
                maxBytes=4096,
                publisherGates=(1, 2, 3, 4),
            ),
            PublishedArtifactDefinition(
                path=fixture.suite_publication,
                mediaType="application/json",
                maxBytes=4096,
                publisherGates=(2,),
            ),
            PublishedArtifactDefinition(
                path=fixture.coverage_publication,
                mediaType="application/json",
                maxBytes=4096,
                publisherGates=(2,),
            ),
            PublishedArtifactDefinition(
                path=fixture.e2e_publication,
                mediaType="application/json",
                maxBytes=4096,
                publisherGates=(4,),
            ),
        ),
    )
    return profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})


def _require_complete_fixture_source(fixture: FixtureRepository) -> None:
    declared = {REPOSITORY_ROOT / path for path in fixture.source_files}
    observed = {path for path in fixture.source_root.rglob("*") if path.is_file()}
    if observed != declared:
        missing = sorted(path.as_posix() for path in observed - declared)
        unavailable = sorted(path.as_posix() for path in declared - observed)
        raise ValueError(
            "portable fixture source inventory is incomplete or stale: "
            f"undeclared={missing}, unavailable={unavailable}"
        )


def fixture_execution_manifest(
    fixture: FixtureRepository,
    *,
    candidate_tree: str,
    mode: ProfileMode = "full",
) -> dict[str, object]:
    """Compile the exact manifest consumed by the real portable Dagger proof."""

    profile = fixture_profile(fixture)
    canonical = canonicalize_repository_profile(profile)
    selection_id = "closeout-targeted" if mode == "targeted" else "closeout-full"
    plan = compile_repository_profile_plan(
        canonical,
        selection_id=selection_id,
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
    )
    return {
        "schemaVersion": "repository-certification-admission/v1",
        "candidateTree": candidate_tree,
        "profile": {"profileDigest": canonical.profileDigest},
        "profilePlan": plan.model_dump(mode="json"),
        "executorAdapter": profile.executorAdapters[0].model_dump(mode="json"),
        "resultDecoder": profile.resultDecoders[0].model_dump(mode="json"),
        "publishedArtifacts": [
            artifact.model_dump(mode="json") for artifact in profile.publishedArtifacts
        ],
    }


def _selection(selection_id, purpose, mode, rail_ids) -> RepositoryProfileSelection:
    gates = tuple(
        RepositoryGateSelection(
            gate=gate,
            status="applicable",
            railIds=tuple(identity for identity in rail_ids if _gate(identity) == gate),
            selectionIdentity=f"{selection_id}:gate-{gate}",
            population=f"declared Gate {gate} population",
        )
        for gate in (1, 2, 3, 4)
    )
    return RepositoryProfileSelection(
        selectionId=selection_id,
        purpose=purpose,
        mode=mode,
        executorAdapterId="certifying-dagger",
        resultDecoderId="terminal-result",
        gates=gates,
    )


def _gate(identity: RailIdentity) -> int:
    return {
        "static-quality": 1,
        "ordinary-suite": 2,
        "post-suite-quality": 3,
        "clean-room-e2e": 4,
    }[identity.railId]


def _rail(fixture: FixtureRepository, spec: _RailSpec) -> RepositoryRailDefinition:
    return RepositoryRailDefinition(
        identity=RailIdentity(railId=spec.rail_id, version="1.0.0"),
        gate=spec.gate,
        railClass=spec.rail_class,
        ownerClass="repository-maintainer",
        correctiveOwner="repository-maintainer",
        posture="enforcing",
        orderKey=spec.rail_id,
        prerequisites=tuple(
            RailIdentity(railId=identity, version="1.0.0") for identity in spec.prerequisites
        ),
        requiredArtifacts=spec.required_artifacts,
        runtimeInputs=RailRuntimeInputs(
            runtimeIdentity=fixture.runtime_identity,
            toolchainDigest=content_digest({"runtimeIdentity": fixture.runtime_identity}),
            imageDigest=fixture.image_reference.rsplit("@sha256:", 1)[1],
            lockDigest=content_digest(
                {
                    path.as_posix(): _file_digest(REPOSITORY_ROOT / path)
                    for path in fixture.runtime_manifests
                }
            ),
            environmentDigest=content_digest({"required": ["CI"]}),
            secretPolicyDigest=content_digest({"policy": "no-secrets"}),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{spec.rail_id}-evidence",
                mediaType="application/json",
                maxBytes=4096,
            ),
        ),
        outputArtifacts=tuple(
            ArtifactDeclaration(
                artifactId=artifact,
                schemaVersion="fixture-artifact/v1",
                mediaType="application/json",
            )
            for artifact in spec.output_artifacts
        ),
        execution=RepositoryRailExecution(
            adapterId=f"{spec.rail_id}-command",
            workingDirectory=".",
            command=spec.command,
            environmentContract=("CI",),
            scopeProviderId=spec.selector,
            inputSelectors=("tracked-inputs",) if spec.selector else (),
            resultDecoderId="terminal-result",
            timeoutSeconds=600,
            resourcePolicyId="fixture-bounded",
            cleanRoom=spec.clean_room,
            teardownPolicy="always" if spec.clean_room else "not-required",
            executionEvidence=f"profile://rails/{spec.rail_id}",
        ),
    )


__all__ = [
    "NODE_FIXTURE",
    "RUST_FIXTURE",
    "dagger_runtime_digest",
    "fixture_execution_manifest",
    "fixture_profile",
]
