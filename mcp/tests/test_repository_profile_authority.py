"""Certification profile path authority refuses absence, ambiguity, and escape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents_remember.certification.repository_profiles import load_repository_profile
from agents_remember.errors import CertificationProfileError
from repository_profile_test_support import fixture_profile


def _codes(error: CertificationProfileError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


def _write_profile(root: Path) -> Path:
    path = root / "certification" / "profile.json"
    path.parent.mkdir(parents=True)
    path.write_text(fixture_profile().model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def test_exact_repository_relative_authority_loads_one_file(tmp_path: Path) -> None:
    root = tmp_path / "fixture-node"
    root.mkdir()
    source = _write_profile(root)

    admitted = load_repository_profile(
        "fixture-node",
        root,
        "certification/profile.json",
    )

    assert admitted.source_path == source.resolve()
    assert admitted.canonical.profileDigest == fixture_profile().profileDigest


@pytest.mark.parametrize(
    ("reference", "code"),
    (
        (None, "profile-authority-missing"),
        (["one.json", "two.json"], "profile-authority-ambiguous"),
        ("../outside.json", "profile-path-invalid"),
        ("./profile.json", "profile-path-invalid"),
        ("nested//profile.json", "profile-path-invalid"),
        ("nested/", "profile-path-invalid"),
        ("/absolute/profile.json", "profile-path-invalid"),
        ("C:/absolute/profile.json", "profile-path-invalid"),
        ("missing.json", "profile-file-unavailable"),
    ),
)
def test_invalid_authority_refuses_before_read(
    tmp_path: Path,
    reference: object,
    code: str,
) -> None:
    root = tmp_path / "fixture-node"
    root.mkdir()

    with pytest.raises(CertificationProfileError) as raised:
        load_repository_profile("fixture-node", root, reference)

    assert code in _codes(raised.value)
    assert raised.value.status == "certification-profile-invalid"


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "fixture-node"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (root / "profile.json").symlink_to(outside)

    with pytest.raises(CertificationProfileError) as raised:
        load_repository_profile("fixture-node", root, "profile.json")

    assert _codes(raised.value) == {"profile-path-symlink"}


def test_malformed_and_digest_mismatch_are_typed(tmp_path: Path) -> None:
    root = tmp_path / "fixture-node"
    root.mkdir()
    profile = root / "profile.json"
    profile.write_text("{", encoding="utf-8")
    with pytest.raises(CertificationProfileError) as malformed:
        load_repository_profile("fixture-node", root, "profile.json")
    assert _codes(malformed.value) == {"profile-json-invalid"}

    payload = fixture_profile().model_dump(mode="json")
    payload["profileDigest"] = "f" * 64
    profile.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CertificationProfileError) as mismatch:
        load_repository_profile("fixture-node", root, "profile.json")
    assert _codes(mismatch.value) == {"profile-digest-mismatch"}


def test_repository_identity_mismatch_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "another-repository"
    root.mkdir()
    _write_profile(root)

    with pytest.raises(CertificationProfileError) as raised:
        load_repository_profile("another-repository", root, "certification/profile.json")

    assert _codes(raised.value) == {"profile-repository-mismatch"}
