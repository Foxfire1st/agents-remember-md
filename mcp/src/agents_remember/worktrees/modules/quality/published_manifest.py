"""Strict reader for the atomic current-quality-generation manifest."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, Literal

from pydantic import ValidationError

from agents_remember.certification.repository_profiles.models import (
    JsonExitStatusDecoderDefinition,
)
from agents_remember.models.lifecycles.evidence_dependencies import (
    EVIDENCE_DEPENDENCY_VALIDATOR,
    EvidenceDependencies,
    build_evidence_dependencies,
    canonical_sha256,
    dependency,
    require_evidence_dependencies,
)

QUALITY_MANIFEST_SCHEMA_VERSION: Final[Literal["3.1"]] = "3.1"
RUNTIME_AUTHORITY_ROOT_FIELD = "runtimeAuthorityDigest"
REPORT_SET_MANIFEST = "quality-report-set.json"
_MANIFEST_ERROR = "no complete Dagger report generation is published"
_ALLOWED_ROOT_FIELDS = frozenset(
    {
        "schemaVersion",
        "generation",
        "candidateTree",
        "profileDigest",
        "profilePlanDigest",
        "profileSelectionId",
        "executorAdapterId",
        "resultDecoder",
        "files",
        "attestation",
        "dependencies",
        "runtimeAuthorityDigest",
    }
)
_GENERATION_DIGEST_FIELDS = frozenset(
    {
        "candidateTree",
        "profileDigest",
        "profilePlanDigest",
        "profileSelectionId",
        "executorAdapterId",
        "resultDecoder",
        "files",
        "dependencies",
        "runtimeAuthorityDigest",
    }
)
_PROFILE_IDENTITY_FIELDS = frozenset(
    {
        "profileDigest",
        "profilePlanDigest",
        "profileSelectionId",
        "executorAdapterId",
        "resultDecoder",
    }
)


class PublishedQualityManifestError(RuntimeError):
    """The atomic quality pointer is unreadable or structurally invalid."""


@dataclass(frozen=True)
class PublishedQualityFile:
    sha256: str
    size: int


@dataclass(frozen=True)
class PublishedQualityManifest:
    schema_version: Literal["3.1"]
    generation: str
    candidate_tree: str
    profile_digest: str
    profile_plan_digest: str
    profile_selection_id: str
    executor_adapter_id: str
    result_decoder: JsonExitStatusDecoderDefinition
    files: Mapping[str, PublishedQualityFile]
    attestation: Mapping[str, str] | None
    dependencies: EvidenceDependencies
    runtime_authority_digest: str | None

    def require_file(self, name: str) -> PublishedQualityFile:
        """Return one declared file without constructing an unverified path."""

        try:
            return self.files[name]
        except KeyError as error:
            raise PublishedQualityManifestError(_MANIFEST_ERROR) from error


def published_manifest_payload(manifest: PublishedQualityManifest) -> dict[str, object]:
    """Serialize the complete accepted snapshot for an exact retained-generation binding."""
    payload: dict[str, object] = {
        "schemaVersion": manifest.schema_version,
        "generation": manifest.generation,
        "candidateTree": manifest.candidate_tree,
        "profileDigest": manifest.profile_digest,
        "profilePlanDigest": manifest.profile_plan_digest,
        "profileSelectionId": manifest.profile_selection_id,
        "executorAdapterId": manifest.executor_adapter_id,
        "resultDecoder": manifest.result_decoder.model_dump(mode="json"),
        "files": {
            name: {"sha256": record.sha256, "size": record.size}
            for name, record in manifest.files.items()
        },
        "dependencies": manifest.dependencies.model_dump(mode="json"),
    }
    if manifest.attestation is not None:
        payload["attestation"] = dict(manifest.attestation)
    if manifest.runtime_authority_digest is not None:
        payload["runtimeAuthorityDigest"] = manifest.runtime_authority_digest
    return payload


def load_published_quality_manifest(destination: Path) -> PublishedQualityManifest:
    """Read and validate the sole pointer to the current immutable generation."""

    try:
        raw: object = json.loads((destination / REPORT_SET_MANIFEST).read_text(encoding="utf-8"))
        return parse_published_quality_manifest(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise PublishedQualityManifestError(_MANIFEST_ERROR) from error


def parse_published_quality_manifest(raw: object) -> PublishedQualityManifest:
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

    profile_digest = _digest_field(raw, "profileDigest")
    profile_plan_digest = _digest_field(raw, "profilePlanDigest")
    profile_selection_id = _nonblank_field(raw, "profileSelectionId")
    executor_adapter_id = _nonblank_field(raw, "executorAdapterId")
    try:
        result_decoder = JsonExitStatusDecoderDefinition.model_validate(raw.get("resultDecoder"))
    except ValidationError as error:
        raise ValueError("quality manifest result decoder is invalid") from error

    files = _parse_files(raw.get("files"))
    if result_decoder.artifactPath not in files:
        raise ValueError("quality manifest result decoder names no published file")
    attestation = _parse_attestation(raw.get("attestation"))
    runtime_authority_digest = _parse_runtime_authority_digest(
        raw.get(RUNTIME_AUTHORITY_ROOT_FIELD)
    )
    profile_identity = {
        "profileDigest": profile_digest,
        "profilePlanDigest": profile_plan_digest,
        "profileSelectionId": profile_selection_id,
        "executorAdapterId": executor_adapter_id,
        "resultDecoder": result_decoder.model_dump(mode="json"),
    }
    dependencies = _parse_dependencies(
        raw,
        candidate_tree,
        files,
        profile_identity,
        _ManifestBindings(
            attestation=attestation,
            runtime_authority_digest=runtime_authority_digest,
        ),
    )
    expected_generation = quality_generation_digest(
        {
            "candidateTree": candidate_tree,
            **profile_identity,
            "files": raw.get("files"),
            "dependencies": dependencies.model_dump(mode="json"),
            "runtimeAuthorityDigest": runtime_authority_digest,
        }
    )
    if generation != expected_generation:
        raise ValueError("quality manifest generation id does not match its bound fields")

    return PublishedQualityManifest(
        schema_version=QUALITY_MANIFEST_SCHEMA_VERSION,
        generation=generation,
        candidate_tree=candidate_tree,
        profile_digest=profile_digest,
        profile_plan_digest=profile_plan_digest,
        profile_selection_id=profile_selection_id,
        executor_adapter_id=executor_adapter_id,
        result_decoder=result_decoder,
        files=files,
        attestation=attestation,
        dependencies=dependencies,
        runtime_authority_digest=runtime_authority_digest,
    )


class _ManifestBindings:
    """Admission-order bindings packed behind one argument at the parse boundary."""

    def __init__(
        self,
        *,
        attestation: Mapping[str, str] | None,
        runtime_authority_digest: str | None,
    ) -> None:
        self.attestation = attestation
        self.runtime_authority_digest = runtime_authority_digest


def _parse_dependencies(
    raw: dict[object, object],
    candidate_tree: str,
    files: Mapping[str, PublishedQualityFile],
    profile_identity: Mapping[str, object],
    bindings: _ManifestBindings,
) -> EvidenceDependencies:
    dependencies = EvidenceDependencies.model_validate(raw.get("dependencies"))
    require_evidence_dependencies(dependencies, record_type="quality-report/v2")
    expected = quality_report_dependencies(
        candidate_tree,
        {name: {"sha256": record.sha256, "size": record.size} for name, record in files.items()},
        bindings.attestation,
        profile_identity,
        bindings.runtime_authority_digest,
    )
    if dependencies != expected:
        raise ValueError("quality manifest dependencies do not match its canonical inputs")
    return dependencies


def _parse_files(raw: object) -> Mapping[str, PublishedQualityFile]:
    if not isinstance(raw, dict):
        raise ValueError("quality manifest files must be an object")
    files = {
        name: _parse_file(name, record) for name, record in raw.items() if isinstance(name, str)
    }
    if len(files) != len(raw):
        raise ValueError("quality manifest file names must be strings")
    return MappingProxyType(files)


def quality_generation_digest(fields: Mapping[str, object]) -> str:
    """Bind every semantic pointer field that selects one immutable generation."""

    if set(fields) != _GENERATION_DIGEST_FIELDS:
        raise ValueError("quality generation digest fields are incomplete or ambiguous")
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_attestation(raw: object) -> Mapping[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()
    ):
        raise ValueError("quality manifest attestation must contain string pairs")
    return MappingProxyType(dict(raw))


def _parse_runtime_authority_digest(raw: object) -> str | None:
    """One optional host-authority digest; when present it must be a safe digest."""
    if raw is None:
        return None
    if not isinstance(raw, str) or re.fullmatch(r"[0-9a-f]{64}", raw) is None:
        raise ValueError("quality manifest runtime authority digest is invalid")
    return raw


def _digest_field(raw: Mapping[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"quality manifest {name} is invalid")
    return value


def _nonblank_field(raw: Mapping[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"quality manifest {name} is invalid")
    return value


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
    profile_identity: Mapping[str, object],
    runtime_authority_digest: str | None = None,
) -> EvidenceDependencies:
    """Declare the exact candidate, execution, and report bytes read by consumers."""

    if set(profile_identity) != _PROFILE_IDENTITY_FIELDS:
        raise ValueError("quality profile dependency fields are incomplete or ambiguous")

    file_values = {
        name: dict(record) if isinstance(record, Mapping) else record
        for name, record in sorted(files.items())
    }
    runtime_authority = (
        []
        if runtime_authority_digest is None
        else [dependency("admission", "shared-dagger-authority", runtime_authority_digest)]
    )
    return build_evidence_dependencies(
        "quality-report/v2",
        [
            dependency("code-tree", "candidate", candidate_tree, algorithm="git-object"),
            dependency("rail-execution", "report-set", canonical_sha256(file_values)),
            dependency(
                "rail-plan",
                "repository-profile-execution",
                canonical_sha256(dict(profile_identity)),
            ),
            *runtime_authority,
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
