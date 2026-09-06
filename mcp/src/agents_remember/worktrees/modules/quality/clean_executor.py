"""Exact staged-candidate execution through the pinned Dagger quality module."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.adapters import (
    DaggerModuleExecutorAdapter,
    JsonExitStatusDecoder,
    RepositoryExecutionRequest,
)
from agents_remember.certification.repository_profiles.authority import (
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.execution import (
    AdmittedRepositoryProfileExecution,
    admit_repository_profile_execution,
)
from agents_remember.certification.repository_profiles.models import (
    JsonExitStatusDecoderDefinition,
    ProfileMode,
    PublishedArtifactDefinition,
)
from agents_remember.errors import CertificationExecutorPrerequisiteError
from agents_remember.kernel.atomic_write import (
    atomic_write_bytes,
    atomic_write_text,
)
from agents_remember.kernel.git_command import run_git
from agents_remember.kernel.platform_subprocess import (
    native_command,
    native_path_environment,
    windows_interop_reason,
)
from agents_remember.models.test_evidence import (
    CertifyingTestEvidence,
    EvidenceConsumer,
    _certifying_evidence_from_verified_dagger,
    require_certifying_evidence,
)
from agents_remember.worktrees.modules.quality.certification_evidence import (
    protected_certificate_generations,
)
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.modules.quality.dagger_authority import (
    AdmittedDaggerAuthority,
    AuthorityRegistry,
    DaggerOwner,
    admit_dagger_authority,
    authority_environment,
    current_process_fingerprint,
    default_registry_root,
    owner_identity,
    release_dagger_authority,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    QUALITY_MANIFEST_SCHEMA_VERSION,
    REPORT_SET_MANIFEST,
    PublishedQualityManifest,
    load_published_quality_manifest,
    quality_generation_digest,
    quality_report_dependencies,
    require_real_directory_or_missing,
    require_real_file_or_missing,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    _prune_report_generations,
    preflight_report_destination,
    published_report_path_from_manifest,
    remove_legacy_report_projection,
    report_tree_inventory,
)

CLEAN_SANDBOX_NAME = "test-sandbox"
DAGGER_PROGRESS_MAX_BYTES = 512 * 1024
DAGGER_RESULT_MAX_BYTES = 128 * 1024
DAGGER_STREAM_CHUNK_BYTES = 64 * 1024
DAGGER_PROGRESS_TRUNCATION = "[older Dagger output truncated]\n"
REPORT_GENERATIONS_DIRECTORY = ".quality-report-generations"
CommandRunner = Callable[[list[str], Path, Mapping[str, str]], subprocess.CompletedProcess[str]]
ExecutorResolver = Callable[[Mapping[str, str]], str]


@dataclass(frozen=True)
class CleanQualityRequest:
    code_worktree: Path
    worktree_group: Path
    repository_id: str
    profile_reference: Path | None
    mode: ProfileMode
    diff_base: str
    memory_cap_bytes: int | None = None
    attestation: Mapping[str, str] | None = None


@dataclass(frozen=True)
class CleanQualityOutcome:
    """Dagger process result paired with governed test evidence when it published."""

    process: subprocess.CompletedProcess[str]
    evidence: CertifyingTestEvidence | None
    manifest: PublishedQualityManifest | None

    @property
    def args(self) -> object:
        return self.process.args

    @property
    def returncode(self) -> int:
        return self.process.returncode

    @property
    def stdout(self) -> str | None:
        return self.process.stdout

    @property
    def stderr(self) -> str | None:
        return self.process.stderr


@dataclass(frozen=True)
class _PreparedSandbox:
    root: Path
    candidate_tree: str
    head: str
    staged_overlay_sha256: str
    bundle_sha256: str


def clean_sandbox_root(worktree_group: Path) -> Path:
    return worktree_group / "reports" / CLEAN_SANDBOX_NAME


def run_clean_quality(
    request: CleanQualityRequest,
    *,
    runner: CommandRunner | None = None,
    executor_resolver: ExecutorResolver | None = None,
    authority: AdmittedDaggerAuthority | None = None,
) -> CleanQualityOutcome:
    """Run the exact profile-declared Dagger adapter; never fall back to host rails.

    Every launch crosses the one host-level shared Dagger authority boundary: the
    declared connection-only runner and reusable layer store are admitted and inspected
    before any Dagger command starts, and the immutable authority digest is bound into
    the candidate sandbox manifest and the published quality manifest. Pass an
    authority explicitly to reuse one frozen snapshot (retry/recovery); otherwise
    the host declaration is admitted and the exact live owner is released when the run
    terminalizes.
    """
    if request.mode not in {"targeted", "full"}:
        raise ValueError(f"unknown clean quality mode: {request.mode}")
    for path in (request.code_worktree, request.worktree_group):
        if reason := windows_interop_reason(path):
            raise RuntimeError(f"clean quality refuses {path}: {reason}")
    admitted = authority
    registry: AuthorityRegistry | None = None
    if admitted is None:
        registry = AuthorityRegistry(default_registry_root())
        admitted = admit_dagger_authority(
            registry=registry,
            owner_factory=lambda snapshot: _quality_owner(request, snapshot),
        )
    prepared = _prepare_sandbox(request)
    source = prepared.root / "source"
    profile_execution = _admit_prepared_profile(request, prepared)
    _write_sandbox_manifest(request, prepared, profile_execution, admitted)
    env, command = _executor_command(
        request,
        prepared,
        profile_execution,
        executor_resolver=executor_resolver,
        authority=admitted,
    )
    execute = runner or (
        lambda command, cwd, command_env: _stream_dagger(
            command,
            cwd,
            command_env,
            progress_path=request.worktree_group / "reports" / "dagger-progress.log",
        )
    )
    atomic_write_text(request.worktree_group / "reports" / "dagger-progress.log", "")
    _write_current(
        request.worktree_group,
        "executor",
        f"start admitted repository adapter {profile_execution.executor.adapterId}",
    )
    try:
        try:
            exported = execute(
                command,
                source,
                env,
            )
        except OSError as error:
            _write_current(
                request.worktree_group,
                "failed",
                "admitted repository executor could not start",
                status="failed",
            )
            raise _executor_prerequisite_failure(profile_execution) from error
        if exported.returncode != 0:
            _write_current(
                request.worktree_group, "failed", "Dagger pipeline/export failed", status="failed"
            )
            return CleanQualityOutcome(exported, None, None)
        return _publish_executor_outcome(
            request,
            prepared,
            profile_execution,
            exported,
            _ReportBindings(
                attestation=request.attestation,
                runtime_authority_digest=admitted.snapshot_digest,
            ),
        )
    finally:
        if registry is not None and admitted.owner is not None:
            release_dagger_authority(admitted, registry=registry)


def _admit_prepared_profile(
    request: CleanQualityRequest,
    prepared: _PreparedSandbox,
) -> AdmittedRepositoryProfileExecution:
    admitted = load_repository_profile(
        request.repository_id,
        prepared.root / "source",
        request.profile_reference,
    )
    return admit_repository_profile_execution(
        admitted,
        purpose="closeout",
        mode=request.mode,
        candidate_identity=CandidateIdentity(kind="git-tree", value=prepared.candidate_tree),
    )


def _executor_command(
    request: CleanQualityRequest,
    prepared: _PreparedSandbox,
    execution: AdmittedRepositoryProfileExecution,
    *,
    executor_resolver: ExecutorResolver | None,
    authority: AdmittedDaggerAuthority,
) -> tuple[dict[str, str], list[str]]:
    try:
        native = native_path_environment(os.environ)
        env = authority_environment(authority.snapshot, base=native)
        executable = (
            executor_resolver(env)
            if executor_resolver is not None
            else _resolve_executor(execution.executor.executable, env)
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise _executor_prerequisite_failure(execution) from error
    executor = execution.executor.model_copy(update={"executable": executable})
    command = DaggerModuleExecutorAdapter().command(
        executor,
        RepositoryExecutionRequest(
            candidate_source=prepared.root / "source",
            repository_bundle=prepared.root / "candidate.bundle",
            execution_manifest=prepared.root / "manifest.json",
            mode=execution.selection.mode,
            diff_base=request.diff_base,
            export_root=prepared.root / "export",
            memory_cap_bytes=request.memory_cap_bytes,
        ),
    )
    return env, list(command)


def _publish_executor_outcome(
    request: CleanQualityRequest,
    prepared: _PreparedSandbox,
    profile_execution: AdmittedRepositoryProfileExecution,
    exported: subprocess.CompletedProcess[str],
    bindings: _ReportBindings,
) -> CleanQualityOutcome:
    export_root = prepared.root / "export"
    _publish_reports(
        export_root,
        request.worktree_group / "reports",
        candidate_tree=prepared.candidate_tree,
        profile_execution=profile_execution,
        bindings=bindings,
    )
    manifest = load_published_quality_manifest(request.worktree_group / "reports")
    published_report_path_from_manifest(
        request.worktree_group / "reports",
        manifest,
        manifest.result_decoder.artifactPath,
    )
    pipeline_exit = _exported_pipeline_exit(
        published_generation_root(request.worktree_group / "reports", manifest),
        manifest.result_decoder,
        manifest.files,
    )
    evidence = (
        _certifying_evidence_from_verified_dagger(
            candidate_tree=prepared.candidate_tree,
            result_sha256=_published_result_sha256(manifest),
        )
        if pipeline_exit == 0
        else None
    )
    outcome = "passed" if pipeline_exit == 0 else "failed"
    _write_current(
        request.worktree_group,
        "complete" if pipeline_exit == 0 else "failed",
        f"repository certification adapter {outcome}",
        status="completed" if pipeline_exit == 0 else "failed",
    )
    return CleanQualityOutcome(
        subprocess.CompletedProcess(
            exported.args,
            pipeline_exit,
            stdout=_bounded_text_tail(exported.stdout, max_bytes=DAGGER_RESULT_MAX_BYTES),
            stderr=exported.stderr,
        ),
        evidence,
        manifest,
    )


def _prepare_sandbox(request: CleanQualityRequest) -> _PreparedSandbox:
    sandbox = clean_sandbox_root(request.worktree_group)
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    source = sandbox / "source"
    _git_ok(
        run_git(
            source,
            [
                "clone",
                "--no-local",
                "--no-checkout",
                request.code_worktree.as_posix(),
                source.as_posix(),
            ],
            work_dir=sandbox,
        ),
        "clone exact candidate",
    )
    head = _git_ok(run_git(request.code_worktree, ["rev-parse", "HEAD"]), "resolve HEAD")
    _git_ok(run_git(source, ["checkout", "--detach", head]), "checkout exact HEAD")
    staged = _git_ok(
        run_git(request.code_worktree, ["diff", "--cached", "--binary", "HEAD"]),
        "read staged overlay",
        preserve_output=True,
    )
    if staged:
        _git_ok(run_git(source, ["apply", "--index", "-"], input_text=staged), "apply overlay")
    candidate_tree = _git_ok(run_git(source, ["write-tree"]), "resolve candidate tree")
    bundle = sandbox / "candidate.bundle"
    _git_ok(
        run_git(source, ["bundle", "create", bundle.as_posix(), "HEAD"]),
        "bundle candidate ancestry",
    )
    return _PreparedSandbox(
        root=sandbox,
        candidate_tree=candidate_tree,
        head=head,
        staged_overlay_sha256=hashlib.sha256(staged.encode("utf-8")).hexdigest(),
        bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
    )


def _write_sandbox_manifest(
    request: CleanQualityRequest,
    prepared: _PreparedSandbox,
    execution: AdmittedRepositoryProfileExecution,
    authority: AdmittedDaggerAuthority,
) -> None:
    source = prepared.root / "source"
    manifest = {
        "schemaVersion": "repository-certification-admission/v1",
        "head": prepared.head,
        "stagedOverlaySha256": prepared.staged_overlay_sha256,
        "source": request.code_worktree.as_posix(),
        "bundleSha256": prepared.bundle_sha256,
        "candidateTree": prepared.candidate_tree,
        "profile": {
            "configuredReference": request.profile_reference.as_posix()
            if request.profile_reference is not None
            else None,
            "sourcePath": execution.admitted.source_path.relative_to(source).as_posix(),
            "sourceSha256": execution.admitted.source_sha256,
            "profileDigest": execution.admitted.canonical.profileDigest,
        },
        "profilePlan": execution.plan.model_dump(mode="json"),
        "executorAdapter": execution.executor.model_dump(mode="json"),
        "resultDecoder": execution.decoder.model_dump(mode="json"),
        "runtimeAuthority": authority.snapshot.as_manifest(),
        "publishedArtifacts": [
            artifact.model_dump(mode="json") for artifact in execution.published_artifacts
        ],
    }
    atomic_write_text(
        prepared.root / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _published_result_sha256(manifest: PublishedQualityManifest) -> str:
    return manifest.require_file(manifest.result_decoder.artifactPath).sha256


def _exported_pipeline_exit(
    export_root: Path,
    decoder: JsonExitStatusDecoderDefinition,
    available_artifacts: Collection[str],
) -> int:
    return (
        JsonExitStatusDecoder()
        .decode(
            decoder,
            export_root,
            available_artifacts,
        )
        .exit_code
    )


class ReportBindings:
    """Optional admission-order bindings packed behind one argument."""

    def __init__(
        self,
        *,
        attestation: Mapping[str, str] | None,
        runtime_authority_digest: str | None,
    ) -> None:
        self.attestation = attestation
        self.runtime_authority_digest = runtime_authority_digest


_ReportBindings = ReportBindings


def _publish_reports(
    source: Path,
    destination: Path,
    *,
    candidate_tree: str,
    profile_execution: AdmittedRepositoryProfileExecution,
    bindings: _ReportBindings | None = None,
) -> dict[str, object]:
    """Publish one immutable evidence generation, then atomically point readers at it.

    Individual report renames cannot make a multi-file result atomic. The generation
    directory is complete and hash-validated before the one authoritative pointer moves;
    a crash therefore leaves readers on either the previous complete generation or this
    complete one, never a mixture of both.
    """
    if not source.is_dir():
        raise RuntimeError(f"Dagger did not export its reports directory: {source}")
    if re.fullmatch(r"[0-9a-f]{40,64}", candidate_tree) is None:
        raise RuntimeError("Dagger publication candidate tree is invalid")
    published_artifacts = profile_execution.published_artifacts
    managed_artifacts = profile_execution.admitted.canonical.profile.publishedArtifacts
    exported_names = _validated_export_inventory(source, published_artifacts)
    pipeline_exit = _exported_pipeline_exit(
        source,
        profile_execution.decoder,
        exported_names,
    )
    if pipeline_exit == 0:
        _require_pass_publications(exported_names, published_artifacts)
    managed_files = frozenset(item.path for item in managed_artifacts)
    managed_directories = _declared_report_directories(managed_artifacts)
    preflight_kwargs = {
        "exported_files": managed_files,
        "exported_directories": managed_directories,
    }
    require_real_directory_or_missing(destination, purpose="quality report destination")
    destination.mkdir(parents=True, exist_ok=True)
    require_real_directory_or_missing(destination, purpose="quality report destination")
    files = _report_file_records(source, exported_names)
    profile_identity = _profile_identity(profile_execution)
    bound_digest = None if bindings is None else bindings.runtime_authority_digest
    dependencies = quality_report_dependencies(
        candidate_tree,
        files,
        None if bindings is None else bindings.attestation,
        profile_identity,
        bound_digest,
    )
    dependencies_value = dependencies.model_dump(mode="json")
    generation = _generation_digest(
        candidate_tree,
        files,
        profile_identity,
        dependencies_value,
        bound_digest,
    )
    generations = destination / REPORT_GENERATIONS_DIRECTORY
    require_real_directory_or_missing(generations, purpose="quality generation directory")
    generations.mkdir(parents=True, exist_ok=True)
    generation_root = generations / generation
    previous_generation = _published_generation_or_none(destination)
    protected = protected_certificate_generations(
        destination, store=certificate_store(destination.parent)
    ) | (frozenset() if previous_generation is None else frozenset({previous_generation}))
    _preflight_report_destination(destination, generation_root, **preflight_kwargs)
    _ensure_generation(source, generation_root, files)
    manifest: dict[str, object] = {
        "schemaVersion": QUALITY_MANIFEST_SCHEMA_VERSION,
        "generation": generation,
        "candidateTree": candidate_tree,
        **profile_identity,
        "files": files,
        "dependencies": dependencies_value,
    }
    if bound_digest is not None:
        manifest["runtimeAuthorityDigest"] = bound_digest
    if bindings is not None and bindings.attestation is not None:
        manifest["attestation"] = dict(bindings.attestation)
    _preflight_report_destination(destination, generation_root, **preflight_kwargs)
    _prune_report_generations(
        generations,
        generation,
        protected=protected,
    )
    _remove_legacy_report_projection(
        destination,
        exported_files=managed_files,
        exported_directories=managed_directories,
    )
    _preflight_report_destination(destination, generation_root, **preflight_kwargs)
    atomic_write_text(
        destination / REPORT_SET_MANIFEST,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def _validated_export_inventory(
    source: Path,
    published_artifacts: tuple[PublishedArtifactDefinition, ...],
) -> set[str]:
    exported_names, exported_directories, irregular = report_tree_inventory(source)
    catalog = {item.path: item for item in published_artifacts}
    declared_names = set(catalog)
    declared_directories = _declared_report_directories(published_artifacts)
    unexpected = (
        (exported_names - declared_names)
        | (exported_directories - declared_directories)
        | irregular
    )
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise RuntimeError(f"Dagger exported unexpected report files or directories: {names}")
    oversized = sorted(
        name for name in exported_names if (source / name).stat().st_size > catalog[name].maxBytes
    )
    if oversized:
        raise RuntimeError(f"Dagger artifacts exceed their declared size limits: {oversized}")
    return exported_names


def _require_pass_publications(
    exported_names: set[str],
    published_artifacts: tuple[PublishedArtifactDefinition, ...],
) -> None:
    """Require pass-only publications without erasing a typed failing terminal result."""

    missing = sorted(
        item.path
        for item in published_artifacts
        if item.required and item.path not in exported_names
    )
    if missing:
        raise RuntimeError(f"Dagger omitted required profile artifacts: {missing}")


def _declared_report_directories(
    published_artifacts: tuple[PublishedArtifactDefinition, ...],
) -> frozenset[str]:
    return _report_directories(artifact.path for artifact in published_artifacts)


def _report_directories(names: Iterable[str]) -> frozenset[str]:
    return frozenset(
        parent.as_posix() for name in names for parent in Path(name).parents if parent != Path(".")
    )


def _report_file_records(source: Path, exported_names: set[str]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest(),
            "size": (source / name).stat().st_size,
        }
        for name in sorted(exported_names)
    }


def _generation_digest(
    candidate_tree: str,
    files: Mapping[str, object],
    profile_identity: Mapping[str, object],
    dependencies: object,
    runtime_authority_digest: str | None = None,
) -> str:
    return quality_generation_digest(
        {
            "candidateTree": candidate_tree,
            **profile_identity,
            "files": files,
            "dependencies": dependencies,
            "runtimeAuthorityDigest": runtime_authority_digest,
        }
    )


def _profile_identity(
    profile_execution: AdmittedRepositoryProfileExecution,
) -> dict[str, object]:
    return {
        "profileDigest": profile_execution.admitted.canonical.profileDigest,
        "profilePlanDigest": profile_execution.plan.planDigest,
        "profileSelectionId": profile_execution.selection.selectionId,
        "executorAdapterId": profile_execution.executor.adapterId,
        "resultDecoder": profile_execution.decoder.model_dump(mode="json"),
    }


def _ensure_generation(
    source: Path,
    generation_root: Path,
    files: dict[str, dict[str, object]],
) -> None:
    require_real_directory_or_missing(generation_root, purpose="quality generation")
    if not generation_root.exists():
        staged = Path(
            tempfile.mkdtemp(
                prefix=f".{generation_root.name}.",
                dir=generation_root.parent,
            )
        )
        try:
            for name in sorted(files):
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            _validate_generation(staged, files)
            try:
                staged.rename(generation_root)
            except FileExistsError:
                shutil.rmtree(staged)
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            raise
    else:
        _validate_generation(generation_root, files)


def _preflight_report_destination(
    destination: Path,
    generation_root: Path,
    *,
    exported_files: frozenset[str],
    exported_directories: frozenset[str],
) -> None:
    preflight_report_destination(
        destination,
        generation_root,
        generations_directory=REPORT_GENERATIONS_DIRECTORY,
        exported_files=exported_files,
        exported_directories=exported_directories,
    )


def _published_generation_or_none(destination: Path) -> str | None:
    """Return the exact prior live generation without guessing another authority."""

    pointer = destination / REPORT_SET_MANIFEST
    require_real_file_or_missing(pointer, purpose="quality report pointer")
    if not pointer.exists():
        return None
    return load_published_quality_manifest(destination).generation


def _remove_legacy_report_projection(
    destination: Path,
    *,
    exported_files: frozenset[str],
    exported_directories: frozenset[str],
) -> None:
    remove_legacy_report_projection(
        destination,
        exported_files=exported_files,
        exported_directories=exported_directories,
    )


def published_report_path(destination: Path, name: str) -> Path:
    """Resolve and verify one report from the currently published generation."""
    return published_report_path_from_manifest(
        destination,
        load_published_quality_manifest(destination),
        name,
    )


def published_generation_root(
    destination: Path,
    manifest: PublishedQualityManifest,
) -> Path:
    return destination / REPORT_GENERATIONS_DIRECTORY / manifest.generation


def published_quality_attestation(destination: Path) -> dict[str, str] | None:
    """Return the exact caller-bound identity of the published report generation."""

    manifest = load_published_quality_manifest(destination)
    return None if manifest.attestation is None else dict(manifest.attestation)


def certifying_evidence_from_published_quality(
    destination: Path,
    *,
    candidate_tree: str,
) -> CertifyingTestEvidence:
    """Recover authority only from the verified immutable Dagger generation."""

    manifest = load_published_quality_manifest(destination)
    return certifying_evidence_from_published_manifest(
        destination,
        manifest,
        candidate_tree=candidate_tree,
    )


def certifying_evidence_from_published_manifest(
    destination: Path,
    manifest: PublishedQualityManifest,
    *,
    candidate_tree: str,
) -> CertifyingTestEvidence:
    """Mint from one caller-held immutable generation snapshot."""

    if manifest.candidate_tree != candidate_tree:
        raise RuntimeError(
            "published Dagger evidence targets another candidate tree: "
            f"expected {candidate_tree}, found {manifest.candidate_tree}"
        )
    published_report_path_from_manifest(
        destination,
        manifest,
        manifest.result_decoder.artifactPath,
    )
    if (
        _exported_pipeline_exit(
            published_generation_root(destination, manifest),
            manifest.result_decoder,
            manifest.files,
        )
        != 0
    ):
        raise RuntimeError("published Dagger result did not pass acceptance")
    record = manifest.require_file(manifest.result_decoder.artifactPath)
    return _certifying_evidence_from_verified_dagger(
        candidate_tree=manifest.candidate_tree,
        result_sha256=record.sha256,
    )


def require_published_quality_evidence(
    destination: Path,
    *,
    candidate_tree: str,
    consumer: EvidenceConsumer,
) -> CertifyingTestEvidence:
    """One serialized acceptance firewall shared by lifecycle consumers."""

    evidence = certifying_evidence_from_published_quality(
        destination,
        candidate_tree=candidate_tree,
    )
    return require_certifying_evidence(evidence, consumer=consumer)


def _validate_generation(root: Path, files: dict[str, dict[str, object]]) -> None:
    actual_files, actual_directories, irregular = report_tree_inventory(root)
    expected_files = set(files)
    expected_directories = _report_directories(expected_files)
    if irregular or actual_files != expected_files or actual_directories != expected_directories:
        raise RuntimeError("Dagger report generation contains an undeclared path")
    for name, record in files.items():
        payload = (root / name).read_bytes()
        if (
            len(payload) != record["size"]
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise RuntimeError(f"Dagger report generation copy failed verification: {name}")


def _quality_owner(
    request: CleanQualityRequest,
    snapshot,
) -> DaggerOwner:
    """One exact live owner: operation-kind, task scope, generation, and PID-safe liveness."""
    digest = snapshot.snapshot_digest
    scope = f"{request.repository_id}:{request.worktree_group}:{request.mode}"
    generation = f"quality-run:{request.diff_base or 'no-base'}"
    return DaggerOwner(
        owner_id=owner_identity(
            operation_kind="quality",
            scope=scope,
            generation=generation,
            authority_digest=digest,
        ),
        authority_digest=digest,
        operation_kind="quality",
        scope=scope,
        generation=generation,
        pid=os.getpid(),
        process_fingerprint=current_process_fingerprint(),
        started_at=_now_iso(),
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _git_ok(
    result: subprocess.CompletedProcess[str], action: str, *, preserve_output: bool = False
) -> str:
    if result.returncode != 0:
        raise RuntimeError(f"clean quality could not {action}: {result.stderr.strip()}")
    return result.stdout if preserve_output else result.stdout.strip()


def _write_current(
    worktree_group: Path, step: str, detail: str, *, status: str = "running"
) -> None:
    atomic_write_text(
        worktree_group / "reports" / "quality-progress.json",
        json.dumps({"status": status, "step": step, "detail": detail}, indent=2) + "\n",
    )


def _stream_dagger(
    command: list[str],
    cwd: Path,
    env: Mapping[str, str],
    *,
    progress_path: Path,
) -> subprocess.CompletedProcess[str]:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    prior = _bounded_file_tail(progress_path, max_bytes=DAGGER_PROGRESS_MAX_BYTES)
    progress_tail = bytearray(prior)
    result_tail = bytearray()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        text=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    live = progress_path.open("ab")
    live_size = len(prior)
    truncated = False
    try:
        while chunk := process.stdout.read(DAGGER_STREAM_CHUNK_BYTES):
            if isinstance(chunk, str):  # test doubles must still obey the same byte cap
                chunk = chunk.encode("utf-8", errors="replace")
            _append_bounded_bytes(result_tail, chunk, max_bytes=DAGGER_RESULT_MAX_BYTES)
            before = len(progress_tail)
            _append_bounded_bytes(progress_tail, chunk, max_bytes=DAGGER_PROGRESS_MAX_BYTES)
            truncated = truncated or before + len(chunk) > DAGGER_PROGRESS_MAX_BYTES
            if live_size < DAGGER_PROGRESS_MAX_BYTES:
                writable = chunk[: DAGGER_PROGRESS_MAX_BYTES - live_size]
                _write_live_progress(live, writable)
                live_size += len(writable)
            detail = _chunk_detail(chunk)
            if detail:
                atomic_write_text(
                    progress_path.parent / "quality-progress.json",
                    json.dumps(
                        {"status": "running", "step": "dagger", "detail": detail[:500]},
                        indent=2,
                    )
                    + "\n",
                )
    finally:
        live.close()
    marker = DAGGER_PROGRESS_TRUNCATION.encode("utf-8") if truncated else b""
    retained = _utf8_tail_bytes(
        bytes(progress_tail),
        max_bytes=max(DAGGER_PROGRESS_MAX_BYTES - len(marker), 0),
    )
    atomic_write_bytes(progress_path, marker + retained)
    stdout = _utf8_tail_bytes(bytes(result_tail), max_bytes=DAGGER_RESULT_MAX_BYTES).decode(
        "utf-8", errors="replace"
    )
    return subprocess.CompletedProcess(command, process.wait(), stdout=stdout, stderr="")


def _write_live_progress(live: BinaryIO, chunk: bytes) -> None:
    """Append one already-bounded live chunk without any retained-tail rewrite."""
    live.write(chunk)
    live.flush()


def _append_bounded_bytes(buffer: bytearray, chunk: bytes, *, max_bytes: int) -> None:
    buffer.extend(chunk)
    if len(buffer) > max_bytes:
        del buffer[:-max_bytes]


def _bounded_text_tail(value: str, *, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    return _utf8_tail_bytes(encoded, max_bytes=max_bytes).decode("utf-8")


def _bounded_file_tail(path: Path, *, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(size - max_bytes, 0))
            return _utf8_tail_bytes(source.read(max_bytes), max_bytes=max_bytes)
    except OSError:
        return b""


def _chunk_detail(chunk: bytes) -> str:
    bounded = chunk[-4096:].decode("utf-8", errors="replace")
    lines = [line for line in bounded.splitlines() if line.strip()]
    if not lines:
        return ""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", lines[-1]).strip()[:500]


def _utf8_tail_bytes(value: bytes, *, max_bytes: int) -> bytes:
    if len(value) <= max_bytes:
        return value
    return value[-max_bytes:].decode("utf-8", errors="ignore").encode("utf-8")


def _resolve_executor(executable: str, env: Mapping[str, str]) -> str:
    return native_command([executable], env)[0]


def _executor_prerequisite_failure(
    execution: AdmittedRepositoryProfileExecution,
) -> CertificationExecutorPrerequisiteError:
    """Bind an unavailable shared executor to its earliest affected gate and owners."""

    affected = tuple(gate for gate in execution.plan.gates if gate.applicability == "applicable")
    affected_gates = tuple(gate.gate for gate in affected)
    earliest_gate = affected_gates[0] if affected_gates else 1
    owners = tuple(sorted({rail.correctiveOwner for gate in affected for rail in gate.rails}))
    adapter = execution.executor
    return CertificationExecutorPrerequisiteError(
        "admitted repository certification executor is unavailable at execution",
        (
            {
                "code": "executor-prerequisite-unavailable",
                "path": f"executorAdapters.{adapter.adapterId}.executable",
                "detail": (
                    f"declared executable {adapter.executable!r} cannot start the admitted "
                    "repository adapter"
                ),
                "gate": earliest_gate,
                "affectedGates": affected_gates,
                "correctiveOwners": owners,
                "adapterId": adapter.adapterId,
                "runtimeDigest": adapter.runtimeDigest,
            },
        ),
    )
