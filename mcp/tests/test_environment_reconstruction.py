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


@pytest.mark.parametrize("count", (1, 20))
def test_same_declared_directory_content_preserves_identity_across_roots(tmp_path, count):
    owner = _owner()
    request = _request()
    original, reconstructed = tmp_path / "original", tmp_path / "reconstructed"
    _tree(original, count)
    _tree(reconstructed, count)
    first = owner["build_census"](original, request)
    second = owner["build_census"](reconstructed, request)
    assert first == second
    assert len(first["entries"]) == 2 * count + 3
    assert first["declarationDigest"] == content_digest(request["definition"])
    (reconstructed / "unrelated").write_bytes(b"unrelated candidate-local output")
    assert owner["build_census"](reconstructed, request) == first
    expected = tmp_path / "original.json"
    expected.write_text(json.dumps(first))
    result = owner["verify_census"](reconstructed, request, expected)
    assert result["status"] == "verified"
    assert result["censusDigest"] == first["censusDigest"]


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


@pytest.mark.parametrize("bound", ("maxEntries", "maxFileBytes", "maxBytes"))
def test_complete_stat_bounds_run_before_hashing_any_dependency_file(tmp_path, monkeypatch, bound):
    owner = _owner()
    request = _request()
    _tree(tmp_path / "root")
    request["definition"][bound] = 1

    def forbid_hash(*args, **kwargs):
        raise AssertionError("stat bounds must precede content hashing")

    monkeypatch.setattr(owner["hashlib"], "sha256", forbid_hash)
    with pytest.raises(ValueError, match="bound"):
        owner["build_census"](tmp_path / "root", request)


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


@pytest.mark.parametrize(
    "scopes",
    (
        ("dependencies", "dependencies/cache"),
        ("dependencies", "dependencies-other", "dependencies/cache"),
        ("dependencies", "dependencies"),
        ("../escape",),
    ),
)
def test_environment_declaration_refuses_ambiguous_or_escaping_directory_scopes(scopes):
    request = _request()
    request["definition"]["directoryScopes"] = scopes
    with pytest.raises(ValueError):
        EnvironmentReconstructionDefinition.model_validate(request["definition"])


@pytest.mark.parametrize(
    "identity", ("declaration", "inspection-executable", "runtime", "candidate")
)
def test_original_census_cannot_be_rebound_to_another_authority(tmp_path, identity):
    owner = _owner()
    request = _request()
    root = tmp_path / "root"
    _tree(root)
    expected = tmp_path / "original.json"
    expected.write_text(json.dumps(owner["build_census"](root, request)))
    if identity == "declaration":
        request["definition"]["command"] = ["another-install"]
    elif identity == "inspection-executable":
        request["definition"]["inspectionExecutable"] = "another-inspection-runtime"
    elif identity == "runtime":
        request["runtimeDigest"] = "b" * 64
    else:
        request["candidateIdentity"]["value"] = "d" * 40
    with pytest.raises(ValueError, match="differs from its original"):
        owner["verify_census"](root, request, expected)


@pytest.mark.parametrize("filename", ("ascii.txt", "café.txt"), ids=("ascii", "unicode"))
def test_census_manifest_bound_matches_exact_emitted_utf8_bytes(tmp_path, filename):
    owner = _owner()
    request = _request()
    root = tmp_path / "root"
    _tree(root)
    (root / "generated" / filename).write_bytes(b"dependency")
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    probe = tmp_path / "probe.json"
    assert owner["main"]([str(request_path), str(root), str(probe)]) == 0
    # Measure actual production output. Changing the declared bound changes only
    # fixed-length digest values, so its emitted byte length must stay exact.
    maximum = len(probe.read_bytes())
    request["definition"]["maxManifestBytes"] = maximum
    request_path.write_text(json.dumps(request), encoding="utf-8")
    output = tmp_path / "exact" / "census.json"
    assert owner["main"]([str(request_path), str(root), str(output)]) == 0
    raw = output.read_bytes()
    assert len(raw) == maximum
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert filename.encode("utf-8") in raw
    payload = json.loads(raw.decode("utf-8"))
    assert payload["declarationDigest"] == content_digest(request["definition"])
    assert payload["censusDigest"] == content_digest(
        {key: value for key, value in payload.items() if key != "censusDigest"}
    )
    # Including the trailing newline is material: one byte less must refuse
    # before even creating the output directory or publishing a partial file.
    request["definition"]["maxManifestBytes"] = maximum - 1
    request_path.write_text(json.dumps(request), encoding="utf-8")
    refused = tmp_path / "refused" / "census.json"
    with pytest.raises(ValueError, match="manifest byte bound"):
        owner["main"]([str(request_path), str(root), str(refused)])
    assert not refused.parent.exists()
    assert output.read_bytes() == raw
