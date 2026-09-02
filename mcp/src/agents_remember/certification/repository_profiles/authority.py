"""Confined resolution and admission of one explicit repository profile authority."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from agents_remember.certification.models import RegistryValidationFinding
from agents_remember.certification.repository_profiles.canonical import (
    canonicalize_repository_profile,
)
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    RepositoryCertificationProfile,
)
from agents_remember.certification.repository_profiles.validation import (
    validate_repository_profile,
)
from agents_remember.errors import CertificationProfileError

MAX_PROFILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class AdmittedRepositoryProfile:
    """One exact source file paired with its canonical semantic identity."""

    repository_id: str
    repository_root: Path
    source_path: Path
    source_sha256: str
    canonical: CanonicalRepositoryCertificationProfile


def load_repository_profile(
    repository_id: str,
    repository_root: Path,
    configured_reference: object,
) -> AdmittedRepositoryProfile:
    """Resolve, parse, canonicalize, and validate before any repository rail starts."""

    source_path = resolve_repository_profile_path(repository_root, configured_reference)
    payload = _read_profile_bytes(source_path)
    try:
        profile = RepositoryCertificationProfile.model_validate_json(payload)
    except ValidationError as error:
        _raise_profile_error(
            "repository certification profile schema is invalid",
            tuple(_validation_finding(item) for item in error.errors(include_url=False)),
        )
    try:
        canonical = canonicalize_repository_profile(profile)
    except ValueError as error:
        _raise_profile_error(
            "repository certification profile digest is invalid",
            (
                _finding(
                    "profile-digest-mismatch",
                    "profileDigest",
                    str(error),
                ),
            ),
        )
    if canonical.profile.repositoryId != repository_id:
        _raise_profile_error(
            "repository certification profile authority is invalid",
            (
                _finding(
                    "profile-repository-mismatch",
                    "repositoryId",
                    "profile repository identity does not match the selected repository",
                ),
            ),
        )
    report = validate_repository_profile(canonical)
    if not report.ok:
        _raise_profile_error("repository certification profile is invalid", report.findings)
    return AdmittedRepositoryProfile(
        repository_id=repository_id,
        repository_root=repository_root.resolve(strict=True),
        source_path=source_path,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        canonical=canonical,
    )


def resolve_repository_profile_path(
    repository_root: Path,
    configured_reference: object,
) -> Path:
    """Resolve exactly one real file below the selected repository without discovery."""

    if configured_reference is None:
        _raise_profile_error(
            "repository certification profile authority is missing",
            (
                _finding(
                    "profile-authority-missing",
                    "repositories.certificationProfile",
                    "code certification requires one explicit repository-relative profile",
                ),
            ),
        )
    if not isinstance(configured_reference, (str, Path)):
        _raise_profile_error(
            "repository certification profile authority is ambiguous",
            (
                _finding(
                    "profile-authority-ambiguous",
                    "repositories.certificationProfile",
                    "profile authority must be exactly one repository-relative path",
                ),
            ),
        )
    raw_reference = str(configured_reference)
    reference = Path(raw_reference)
    if (
        reference.is_absolute()
        or not reference.parts
        or (len(reference.parts[0]) >= 2 and reference.parts[0][1] == ":")
        or any(part in {"", ".", ".."} for part in reference.parts)
        or "\\" in raw_reference
        or reference.as_posix() != raw_reference
    ):
        _raise_profile_error(
            "repository certification profile path is invalid",
            (
                _finding(
                    "profile-path-invalid",
                    "repositories.certificationProfile",
                    "profile path must be canonical, relative, and traversal-free",
                ),
            ),
        )
    try:
        root = repository_root.resolve(strict=True)
    except OSError as error:
        _raise_profile_error(
            "repository certification profile root is unavailable",
            (_finding("profile-root-unavailable", "repository", str(error)),),
        )
    candidate = root.joinpath(*reference.parts)
    _reject_symlink_components(root, reference)
    try:
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as error:
        _raise_profile_error(
            "repository certification profile is unavailable",
            (_finding("profile-file-unavailable", "profile", str(error)),),
        )
    if not resolved.is_relative_to(root) or not stat.S_ISREG(mode):
        _raise_profile_error(
            "repository certification profile path escapes its authority",
            (
                _finding(
                    "profile-path-escape",
                    "profile",
                    "profile must be one real regular file inside the selected repository",
                ),
            ),
        )
    return resolved


def _reject_symlink_components(root: Path, reference: Path) -> None:
    current = root
    for part in reference.parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                _raise_profile_error(
                    "repository certification profile path contains a symlink",
                    (
                        _finding(
                            "profile-path-symlink",
                            "profile",
                            "profile authority cannot traverse a symbolic link",
                        ),
                    ),
                )
        except FileNotFoundError:
            return
        except OSError as error:
            _raise_profile_error(
                "repository certification profile path cannot be inspected",
                (_finding("profile-path-unavailable", "profile", str(error)),),
            )


def _read_profile_bytes(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_PROFILE_BYTES:
            _raise_profile_error(
                "repository certification profile exceeds its admission budget",
                (
                    _finding(
                        "profile-size-exceeded",
                        "profile",
                        f"profile is {size} bytes; maximum is {MAX_PROFILE_BYTES}",
                    ),
                ),
            )
        payload = path.read_bytes()
        if len(payload) > MAX_PROFILE_BYTES:
            _raise_profile_error(
                "repository certification profile exceeds its admission budget",
                (
                    _finding(
                        "profile-size-exceeded",
                        "profile",
                        f"profile is {len(payload)} bytes; maximum is {MAX_PROFILE_BYTES}",
                    ),
                ),
            )
        json.loads(payload)
        return payload
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _raise_profile_error(
            "repository certification profile is malformed",
            (_finding("profile-json-invalid", "profile", str(error)),),
        )


def _validation_finding(error: Mapping[str, object]) -> RegistryValidationFinding:
    raw_location = error.get("loc", ())
    location = raw_location if isinstance(raw_location, tuple) else ()
    path = ".".join(str(part) for part in location) or "profile"
    detail = str(error.get("msg") or "profile field is invalid")
    return _finding("profile-schema-invalid", path, detail)


def _finding(code: str, path: str, detail: str) -> RegistryValidationFinding:
    return RegistryValidationFinding(code=code, path=path, detail=detail)


def _raise_profile_error(
    detail: str,
    findings: tuple[RegistryValidationFinding, ...],
) -> Never:
    raise CertificationProfileError(
        detail,
        [finding.model_dump(mode="json") for finding in findings],
    )


__all__ = [
    "AdmittedRepositoryProfile",
    "load_repository_profile",
    "resolve_repository_profile_path",
]
