"""Strict reader for the atomic current-quality-generation manifest."""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, Literal

from agents_remember.models.lifecycles.evidence_dependencies import (
    EVIDENCE_DEPENDENCY_VALIDATOR,
    EvidenceDependencies,
    build_evidence_dependencies,
    canonical_sha256,
    dependency,
    require_evidence_dependencies,
)

QUALITY_MANIFEST_SCHEMA_VERSION: Final[Literal["2.0"]] = "2.0"
REPORT_SET_MANIFEST = "quality-report-set.json"
_MANIFEST_ERROR = "no complete Dagger report generation is published"
_ALLOWED_ROOT_FIELDS = frozenset(
    {"schemaVersion", "generation", "candidateTree", "files", "attestation", "dependencies"}
)


class PublishedQualityManifestError(RuntimeError):
    """The atomic quality pointer is unreadable or structurally invalid."""


@dataclass(frozen=True)
class PublishedQualityFile:
    sha256: str
    size: int


@dataclass(frozen=True)
class PublishedQualityManifest:
    schema_version: Literal["2.0"]
    generation: str
    candidate_tree: str
    files: Mapping[str, PublishedQualityFile]
    attestation: Mapping[str, str] | None
    dependencies: EvidenceDependencies

    def require_file(self, name: str) -> PublishedQualityFile:
        """Return one declared file without constructing an unverified path."""

        try:
            return self.files[name]
        except KeyError as error:
            raise PublishedQualityManifestError(_MANIFEST_ERROR) from error


def load_published_quality_manifest(destination: Path) -> PublishedQualityManifest:
    """Read and validate the sole pointer to the current immutable generation."""

    try:
        raw: object = json.loads((destination / REPORT_SET_MANIFEST).read_text(encoding="utf-8"))
        return _parse_manifest(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise PublishedQualityManifestError(_MANIFEST_ERROR) from error


def _parse_manifest(raw: object) -> PublishedQualityManifest:
    if not isinstance(raw, dict):
        raise ValueError("quality manifest root must be an object")
    if set(raw) - _ALLOWED_ROOT_FIELDS:
        raise ValueError("quality manifest contains unsupported fields")
    if raw.get("schemaVersion") != QUALITY_MANIFEST_SCHEMA_VERSION:
        raise ValueError("quality manifest schema version is unsupported")

    generation = raw.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
        raise ValueError("quality manifest generation id is invalid")

    candidate_tree = raw.get("candidateTree")
    if not isinstance(candidate_tree, str) or not re.fullmatch(r"[0-9a-f]{40,64}", candidate_tree):
        raise ValueError("quality manifest candidate tree is invalid")

    raw_files = raw.get("files")
    if not isinstance(raw_files, dict):
        raise ValueError("quality manifest files must be an object")
    files = {
        name: _parse_file(name, record)
        for name, record in raw_files.items()
        if isinstance(name, str)
    }
    if len(files) != len(raw_files):
        raise ValueError("quality manifest file names must be strings")

    raw_attestation = raw.get("attestation")
    attestation: Mapping[str, str] | None = None
    if raw_attestation is not None:
        if not isinstance(raw_attestation, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_attestation.items()
        ):
            raise ValueError("quality manifest attestation must contain string pairs")
        attestation = MappingProxyType(dict(raw_attestation))

    dependencies = _parse_dependencies(raw, candidate_tree, files, attestation)

    return PublishedQualityManifest(
        schema_version=QUALITY_MANIFEST_SCHEMA_VERSION,
        generation=generation,
        candidate_tree=candidate_tree,
        files=MappingProxyType(files),
        attestation=attestation,
        dependencies=dependencies,
    )


def _parse_dependencies(
    raw: dict[object, object],
    candidate_tree: str,
    files: Mapping[str, PublishedQualityFile],
    attestation: Mapping[str, str] | None,
) -> EvidenceDependencies:
    dependencies = EvidenceDependencies.model_validate(raw.get("dependencies"))
    require_evidence_dependencies(dependencies, record_type="quality-report/v2")
    expected = quality_report_dependencies(
        candidate_tree,
        {name: {"sha256": record.sha256, "size": record.size} for name, record in files.items()},
        attestation,
    )
    if dependencies != expected:
        raise ValueError("quality manifest dependencies do not match its canonical inputs")
    return dependencies


def _parse_file(name: str, raw: object) -> PublishedQualityFile:
    if not is_safe_relative_report_path(name):
        raise ValueError("quality manifest file path is invalid")
    if not isinstance(raw, dict) or set(raw) != {"sha256", "size"}:
        raise ValueError("quality manifest file record is invalid")
    sha256 = raw.get("sha256")
    size = raw.get("size")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("quality manifest file digest is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("quality manifest file size is invalid")
    return PublishedQualityFile(sha256=sha256, size=size)


def quality_report_dependencies(
    candidate_tree: str,
    files: Mapping[str, object],
    attestation: Mapping[str, str] | None,
) -> EvidenceDependencies:
    """Declare the exact candidate, execution, and report bytes read by consumers."""

    file_values = {
        name: dict(record) if isinstance(record, Mapping) else record
        for name, record in sorted(files.items())
    }
    return build_evidence_dependencies(
        "quality-report/v2",
        [
            dependency("code-tree", "candidate", candidate_tree, algorithm="git-object"),
            dependency("rail-execution", "report-set", canonical_sha256(file_values)),
            *(
                dependency("evidence-bytes", name, str(record["sha256"]))
                for name, record in file_values.items()
                if isinstance(record, Mapping)
            ),
            *(
                [dependency("rail-plan", "attestation", canonical_sha256(dict(attestation)))]
                if attestation is not None
                else []
            ),
            dependency(
                "validator",
                EVIDENCE_DEPENDENCY_VALIDATOR,
                canonical_sha256(EVIDENCE_DEPENDENCY_VALIDATOR),
            ),
        ],
    )


def is_safe_relative_report_path(name: str) -> bool:
    if not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and path.as_posix() == name
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def require_real_directory_or_missing(path: Path, *, purpose: str) -> None:
    """Refuse links and irregular nodes at a publication directory boundary."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise PublishedQualityManifestError(
            f"cannot inspect {purpose} before quality publication: {path}"
        ) from error
    if not stat.S_ISDIR(mode):
        raise PublishedQualityManifestError(
            f"unsafe {purpose} before quality publication: {path} must be a real directory"
        )


def require_real_file_or_missing(path: Path, *, purpose: str) -> None:
    """Refuse links and irregular nodes before removing a legacy report file."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise PublishedQualityManifestError(
            f"cannot inspect {purpose} before quality publication: {path}"
        ) from error
    if not stat.S_ISREG(mode):
        raise PublishedQualityManifestError(
            f"unsafe {purpose} before quality publication: {path} must be a regular file"
        )
