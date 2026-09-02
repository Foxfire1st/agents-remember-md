"""Canonical bytes and digest authority for repository certification profiles."""

from __future__ import annotations

from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    RepositoryCertificationProfile,
    _normalize_repository_profile,
    repository_profile_digest,
)


def canonicalize_repository_profile(
    profile: RepositoryCertificationProfile,
) -> CanonicalRepositoryCertificationProfile:
    """Normalize one profile and require its declared digest to authorize those bytes."""

    normalized = _normalize_repository_profile(profile)
    digest = repository_profile_digest(normalized)
    if normalized.profileDigest != digest:
        raise ValueError(
            "repository certification profile digest does not match its canonical content"
        )
    return CanonicalRepositoryCertificationProfile(
        profile=normalized,
        profileDigest=digest,
    )


__all__ = ["canonicalize_repository_profile", "repository_profile_digest"]
