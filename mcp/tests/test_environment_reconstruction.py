"""Original dependency census identity and fail-closed reconstruction contracts."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.repository_profiles.models import (
    EnvironmentReconstructionDefinition,
)
from agents_remember.models.certification.base import RailIdentity
from repository_profile_test_support import REPOSITORY_ROOT


def _owner():
    return runpy.run_path(
        str(REPOSITORY_ROOT / ".dagger/src/agents_remember_quality/environment/census.py")
    )


def _request():
    definition = EnvironmentReconstructionDefinition(
        environmentId="fixture-dependencies",
        producerRail=RailIdentity(railId="static-quality", version="1.0.0"),
        consumingGates=(2, 3, 4),
        directoryScopes=("dependencies", "generated"),
        artifactId="environment-census",
        manifestPath="environment/census.json",
        reconstructionProofPath="environment/reconstruction.json",
        workingDirectory=".",
        command=("fixture-install",),
        inspectionExecutable="python3",
        timeoutSeconds=30,
        maxEntries=100,
        maxBytes=4096,
        maxFileBytes=1024,
        maxManifestBytes=32768,
    )
    return {
        "definition": definition.model_dump(mode="json"),
        "candidateIdentity": {"kind": "git-tree", "value": "c" * 40},
        "runtimeDigest": "a" * 64,
    }


def _tree(root: Path, count: int = 1):
    for scope in ("dependencies", "generated"):
        path = root / scope
        path.mkdir(parents=True)
        for index in range(count):
            (path / f"file-{index}").write_bytes(f"content-{index}".encode())
    (root / "dependencies/link").symlink_to("file-0")


@pytest.mark.parametrize(
    "fault",
    (
        "bytes",
        "mode",
        "missing",
        "extra",
        "symlink",
        "directory-symlink",
        "omitted-row",
        "corrupt-json",
    ),
)
def test_missing_corrupt_or_different_reconstruction_never_matches_original(tmp_path, fault):
    owner = _owner()
    request = _request()
    root = tmp_path / "root"
    _tree(root)
    original = owner["build_census"](root, request)
    expected = tmp_path / "original.json"
    expected.write_text(json.dumps(original))
    target = root / "generated/file-0"
    if fault == "bytes":
        target.write_bytes(b"changed")
    elif fault == "mode":
        target.chmod(0o700)
    elif fault == "missing":
        target.unlink()
    elif fault == "extra":
        (root / "generated/new").write_bytes(b"new")
    elif fault == "symlink":
        target.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(b"content-0")
        target.symlink_to(outside)
    elif fault == "directory-symlink":
        (root / "generated").rename(tmp_path / "outside-dir")
        (root / "generated").symlink_to(tmp_path / "outside-dir", target_is_directory=True)
    elif fault == "omitted-row":
        original["entries"].pop()
        original["censusDigest"] = content_digest(
            {key: value for key, value in original.items() if key != "censusDigest"}
        )
        expected.write_text(json.dumps(original))
    elif fault == "corrupt-json":
        expected.write_bytes(b"{")
    with pytest.raises((ValueError, OSError)):
        owner["verify_census"](root, request, expected)


def test_file_mutation_during_census_cannot_be_certified(tmp_path, monkeypatch):
    owner = _owner()
    request = _request()
    root = tmp_path / "root"
    _tree(root)
    original_open = Path.open
    changed = False

    def mutating_open(path, *args, **kwargs):
        nonlocal changed
        result = original_open(path, *args, **kwargs)
        if path == root / "generated/file-0" and not changed:
            changed = True
            path.chmod(0o700)
        return result

    monkeypatch.setattr(Path, "open", mutating_open)
    with pytest.raises(ValueError, match="changed"):
        owner["build_census"](root, request)
