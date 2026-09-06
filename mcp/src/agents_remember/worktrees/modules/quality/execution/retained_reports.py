"""Bounded copies from accepted original generations into one owned sandbox."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agents_remember.certification.repository_profiles.models import (
    require_relative_repository_file_path,
)
from agents_remember.worktrees.modules.quality.execution.models import CodeCertificationExecution
from agents_remember.worktrees.modules.quality.published_manifest import PublishedQualityManifest
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)

MAX_RETAINED_FILES = 4096


@dataclass(frozen=True)
class RetainedReportFile:
    path: str
    sha256: str
    size: int
    publication: PublishedQualityManifest

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "generation": self.publication.generation,
        }


def retained_report_inventory(
    execution: CodeCertificationExecution,
) -> tuple[RetainedReportFile, ...]:
    """Only selected predecessor result members may enter the transport snapshot."""
    execution.validate()
    declarations = {
        item.path: item
        for item in execution.run.repositoryProfile.profile.publishedArtifacts
        if any(gate < execution.first_gate for gate in item.publisherGates)
    }
    maximum_bytes = sum(item.maxBytes for item in declarations.values())
    files: dict[str, RetainedReportFile] = {}
    total = 0
    for item in execution.retained:
        for rail in item.result.railResults:
            members = (
                *((entry.reference, entry.sha256, entry.size) for entry in rail.evidence),
                *((entry.evidenceRef, entry.sha256, entry.size) for entry in rail.artifacts),
            )
            for path, digest, size in members:
                require_relative_repository_file_path(path)
                declaration = declarations.get(path)
                if declaration is None or item.result.gate not in declaration.publisherGates:
                    raise ValueError("retained report lacks its exact frozen producer publication")
                if size > declaration.maxBytes:
                    raise ValueError("retained report exceeds its frozen publication byte bound")
                bound = item.publication.require_file(path)
                if (bound.sha256, bound.size) != (digest, size):
                    raise ValueError(
                        "retained result differs from its original publication inventory"
                    )
                prior = files.get(path)
                if prior is not None:
                    if (prior.sha256, prior.size) != (digest, size):
                        raise ValueError("retained reports contain conflicting producer paths")
                    continue
                total += size
                files[path] = RetainedReportFile(path, digest, size, item.publication)
                if len(files) > MAX_RETAINED_FILES or total > maximum_bytes:
                    raise ValueError("retained reports exceed the bounded transport population")
    return tuple(files[path] for path in sorted(files))


def snapshot_retained_reports(
    execution: CodeCertificationExecution,
    *,
    reports: Path,
    destination: Path,
) -> tuple[RetainedReportFile, ...]:
    """Verify each selected source, then copy and reverify its exact bounded bytes.

    The caller owns a fresh sandbox directory and its existing cleanup lifecycle.
    No current-generation pointer, history scan, or unrelated publication is read.
    """
    files = retained_report_inventory(execution)
    if execution.first_gate == 1:
        return files
    # Resolve all real source paths before creating any transport file. The accepted
    # resolver rejects unsafe generations, symlink components and mutated source bytes.
    sources = tuple(
        published_report_path_from_manifest(reports, item.publication, item.path) for item in files
    )
    destination.mkdir()
    for item, source_path in zip(files, sources, strict=True):
        with source_path.open("rb") as source:
            raw = source.read(item.size + 1)
        if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.sha256:
            raise ValueError("retained report changed while snapshotting its accepted generation")
        target = destination / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(raw)
    return files
