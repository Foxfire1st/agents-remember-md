"""Exact staged-candidate execution through the pinned Dagger quality module."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

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
from agents_remember.worktrees.modules.quality.published_manifest import (
    QUALITY_MANIFEST_SCHEMA_VERSION,
    REPORT_SET_MANIFEST,
    PublishedQualityManifest,
    load_published_quality_manifest,
    quality_report_dependencies,
    require_real_directory_or_missing,
    require_real_file_or_missing,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    preflight_report_destination,
    remove_legacy_report_projection,
    report_tree_inventory,
)
from agents_remember.worktrees.modules.quality.result_artifacts import (
    validate_result_artifact_references,
)

DAGGER_VERSION = "v0.21.8"
CODEX_VERSION = "0.151.0"
PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright:v1.60.0-noble@"
    "sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9"
)
CLEAN_SANDBOX_NAME = "test-sandbox"
DAGGER_PROGRESS_MAX_BYTES = 512 * 1024
DAGGER_RESULT_MAX_BYTES = 128 * 1024
DAGGER_STREAM_CHUNK_BYTES = 64 * 1024
DAGGER_PROGRESS_TRUNCATION = "[older Dagger output truncated]\n"
REPORT_GENERATIONS_DIRECTORY = ".quality-report-generations"
EXPORTED_REPORT_NAMES = frozenset(
    {
        "ambient-role-chat-e2e/run-1.json",
        "ambient-role-chat-e2e/run-2.json",
        "ambient-role-chat-e2e/summary.json",
        "causal-failures.json",
        "causal-failures.md",
        "clean-quality-results.json",
        "codex-probe.json",
        "coverage.data",
        "coverage.json",
        "pytest-events.jsonl",
        "pytest-phases.json",
        "python-runtime.json",
        "python-venv-runtime.json",
        "quality-progress.json",
    }
)
EXPORTED_REPORT_DIRECTORIES = frozenset(
    parent.as_posix()
    for name in EXPORTED_REPORT_NAMES
    for parent in (Path(name).parent,)
    if parent != Path(".")
)

CommandRunner = Callable[[list[str], Path, Mapping[str, str]], subprocess.CompletedProcess[str]]
DaggerResolver = Callable[[Mapping[str, str]], str]


@dataclass(frozen=True)
class CleanQualityRequest:
    code_worktree: Path
    worktree_group: Path
    mode: str
    diff_base: str
    memory_cap_bytes: int | None = None
    attestation: Mapping[str, str] | None = None


@dataclass(frozen=True)
class CleanQualityOutcome:
    """Dagger process result paired with governed test evidence when it published."""

    process: subprocess.CompletedProcess[str]
    evidence: CertifyingTestEvidence | None

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


def clean_sandbox_root(worktree_group: Path) -> Path:
    return worktree_group / "reports" / CLEAN_SANDBOX_NAME


def run_clean_quality(
    request: CleanQualityRequest,
    *,
    runner: CommandRunner | None = None,
    dagger_resolver: DaggerResolver | None = None,
) -> CleanQualityOutcome:
    """Run the canonical Dagger pipeline; never fall back to host quality rails."""
    if request.mode not in {"targeted", "full"}:
        raise ValueError(f"unknown clean quality mode: {request.mode}")
    for path in (request.code_worktree, request.worktree_group):
        if reason := windows_interop_reason(path):
            raise RuntimeError(f"clean quality refuses {path}: {reason}")
    prepared = _prepare_sandbox(request)
    source = prepared.root / "source"
    bundle = prepared.root / "candidate.bundle"
    export_root = prepared.root / "export"
    env = native_path_environment(os.environ)
    dagger = (dagger_resolver or _resolve_dagger)(env)
    execute = runner or (
        lambda command, cwd, command_env: _stream_dagger(
            command,
            cwd,
            command_env,
            progress_path=request.worktree_group / "reports" / "dagger-progress.log",
        )
    )
    atomic_write_text(request.worktree_group / "reports" / "dagger-progress.log", "")
    common = [
        dagger,
        "--progress=plain",
        "call",
        "quality",
        f"--source={source.as_posix()}",
        f"--repository-bundle={bundle.as_posix()}",
        f"--mode={request.mode}",
    ]
    if request.diff_base:
        common.append(f"--diff-base={request.diff_base}")
    if request.memory_cap_bytes is not None:
        common.append(f"--memory-cap-bytes={request.memory_cap_bytes}")
    _write_current(request.worktree_group, "dagger", "start pinned Ubuntu quality pipeline")
    exported = execute(
        [*common, "reports", "export", f"--path={export_root.as_posix()}"],
        source,
        env,
    )
    if exported.returncode != 0:
        _write_current(
            request.worktree_group, "failed", "Dagger pipeline/export failed", status="failed"
        )
        return CleanQualityOutcome(exported, None)
    pipeline_exit = _exported_pipeline_exit(export_root)
    manifest = _publish_reports(
        export_root,
        request.worktree_group / "reports",
        candidate_tree=prepared.candidate_tree,
        attestation=request.attestation,
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
        f"Dagger Ubuntu quality {outcome}",
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
    manifest = {
        "head": head,
        "stagedOverlaySha256": hashlib.sha256(staged.encode("utf-8")).hexdigest(),
        "source": request.code_worktree.as_posix(),
        "daggerVersion": DAGGER_VERSION,
        "image": PLAYWRIGHT_IMAGE,
        "codexVersion": CODEX_VERSION,
        "bundleSha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "candidateTree": candidate_tree,
    }
    atomic_write_text(sandbox / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return _PreparedSandbox(sandbox, candidate_tree)


def _published_result_sha256(manifest: Mapping[str, object]) -> str:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict):
        raise RuntimeError("published Dagger manifest has no file inventory")
    raw_result = raw_files.get("clean-quality-results.json")
    if not isinstance(raw_result, dict):
        raise RuntimeError("published Dagger manifest has no authoritative result")
    digest = raw_result.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise RuntimeError("published Dagger result digest is invalid")
    return digest


def _exported_pipeline_exit(export_root: Path) -> int:
    report = export_root / "clean-quality-results.json"
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
        exit_code = payload["exitCode"]
        status = payload["status"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(f"Dagger exported no valid authoritative result: {report}") from error
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
        raise RuntimeError(f"Dagger exported an invalid pipeline exit code: {exit_code!r}")
    expected_status = "passed" if exit_code == 0 else "failed"
    if status != expected_status:
        raise RuntimeError(
            f"Dagger exported a contradictory result: status={status!r}, exitCode={exit_code}"
        )
    return exit_code


def _publish_reports(
    source: Path,
    destination: Path,
    *,
    candidate_tree: str,
    attestation: Mapping[str, str] | None = None,
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
    _exported_pipeline_exit(source)
    exported_names = _validated_export_inventory(source)
    require_real_directory_or_missing(destination, purpose="quality report destination")
    destination.mkdir(parents=True, exist_ok=True)
    require_real_directory_or_missing(destination, purpose="quality report destination")
    files = _report_file_records(source, exported_names)
    dependencies = quality_report_dependencies(candidate_tree, files, attestation)
    dependencies_value = dependencies.model_dump(mode="json")
    generation = _generation_digest(candidate_tree, files, dependencies_value)
    generations = destination / REPORT_GENERATIONS_DIRECTORY
    require_real_directory_or_missing(generations, purpose="quality generation directory")
    generations.mkdir(parents=True, exist_ok=True)
    generation_root = generations / generation
    previous_generation = _published_generation_or_none(destination)
    _preflight_report_destination(destination, generation_root)
    _ensure_generation(source, generation_root, files)
    manifest: dict[str, object] = {
        "schemaVersion": QUALITY_MANIFEST_SCHEMA_VERSION,
        "generation": generation,
        "candidateTree": candidate_tree,
        "files": files,
        "dependencies": dependencies_value,
    }
    if attestation is not None:
        manifest["attestation"] = dict(attestation)
    _preflight_report_destination(destination, generation_root)
    _prune_report_generations(
        generations,
        generation,
        protected=(() if previous_generation is None else (previous_generation,)),
    )
    _remove_legacy_report_projection(destination)
    _preflight_report_destination(destination, generation_root)
    atomic_write_text(
        destination / REPORT_SET_MANIFEST,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def _validated_export_inventory(source: Path) -> set[str]:
    exported_names, exported_directories, irregular = report_tree_inventory(source)
    unexpected = (
        (exported_names - EXPORTED_REPORT_NAMES)
        | (exported_directories - EXPORTED_REPORT_DIRECTORIES)
        | irregular
    )
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise RuntimeError(f"Dagger exported unexpected report files or directories: {names}")
    validate_result_artifact_references(source, exported_names)
    return exported_names


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
    dependencies: object,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "candidateTree": candidate_tree,
                "files": files,
                "dependencies": dependencies,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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


def _preflight_report_destination(destination: Path, generation_root: Path) -> None:
    preflight_report_destination(
        destination,
        generation_root,
        generations_directory=REPORT_GENERATIONS_DIRECTORY,
        exported_files=EXPORTED_REPORT_NAMES,
        exported_directories=EXPORTED_REPORT_DIRECTORIES,
    )


def _published_generation_or_none(destination: Path) -> str | None:
    """Return the exact prior live generation without guessing another authority."""

    pointer = destination / REPORT_SET_MANIFEST
    require_real_file_or_missing(pointer, purpose="quality report pointer")
    if not pointer.exists():
        return None
    return load_published_quality_manifest(destination).generation


def _remove_legacy_report_projection(destination: Path) -> None:
    remove_legacy_report_projection(
        destination,
        exported_files=EXPORTED_REPORT_NAMES,
        exported_directories=EXPORTED_REPORT_DIRECTORIES,
    )


def published_report_path(destination: Path, name: str) -> Path:
    """Resolve and verify one report from the currently published generation."""
    return published_report_path_from_manifest(
        destination,
        load_published_quality_manifest(destination),
        name,
    )


def published_report_path_from_manifest(
    destination: Path,
    manifest: PublishedQualityManifest,
    name: str,
) -> Path:
    """Resolve one report against an already accepted immutable generation snapshot."""

    file_record = manifest.require_file(name)
    report = destination / REPORT_GENERATIONS_DIRECTORY / manifest.generation / name
    try:
        payload = report.read_bytes()
    except OSError as error:
        raise RuntimeError(f"published Dagger report is incomplete: {name}") from error
    if (
        len(payload) != file_record.size
        or hashlib.sha256(payload).hexdigest() != file_record.sha256
    ):
        raise RuntimeError(f"published Dagger report failed generation verification: {name}")
    return report


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
    published_result = published_report_path_from_manifest(
        destination,
        manifest,
        "clean-quality-results.json",
    )
    if _exported_pipeline_exit(published_result.parent) != 0:
        raise RuntimeError("published Dagger result did not pass acceptance")
    record = manifest.require_file("clean-quality-results.json")
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
    expected_directories = {
        parent.as_posix()
        for name in expected_files
        for parent in (Path(name).parent,)
        if parent != Path(".")
    }
    if irregular or actual_files != expected_files or actual_directories != expected_directories:
        raise RuntimeError("Dagger report generation contains an undeclared path")
    for name, record in files.items():
        payload = (root / name).read_bytes()
        if (
            len(payload) != record["size"]
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise RuntimeError(f"Dagger report generation copy failed verification: {name}")


def _prune_report_generations(
    generations: Path,
    current: str,
    *,
    protected: Collection[str] = (),
) -> None:
    candidates = [
        path for path in generations.iterdir() if re.fullmatch(r"[0-9a-f]{64}", path.name)
    ]
    for path in candidates:
        require_real_directory_or_missing(path, purpose="published quality generation")
    completed = sorted(
        candidates,
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    keep = {current, *protected, *(path.name for path in completed[:2])}
    for path in completed:
        if path.name not in keep:
            shutil.rmtree(path)


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


def _resolve_dagger(env: Mapping[str, str]) -> str:
    return native_command(["dagger"], env)[0]
