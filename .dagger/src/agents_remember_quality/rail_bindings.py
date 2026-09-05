"""Per-rail terminal evidence/artifact bindings for the exported payload.

The R11 gate-result manifest the host seam publishes binds every rail's
declared evidenceContract and outputArtifacts to immutable bytes.  This
module builds those bindings from bytes the executor actually observed for the
rail -- its captured stdout/stderr tail for evidence, and real container files
for artifacts -- so the payload the executor exports carries evidence and
artifact arrays that no host code ever synthesizes.

Rules (kept here, in one place):

* Evidence: every declared evidenceId of the rail binds the same bounded
  capture of the rail's observed output (digest over the exact bytes, size, and
  an honest container path).  An empty capture is a real empty capture.
* Artifacts: an outputArtifact is bound only when the caller observed the
  real file the artifact's bytes live in (id -> observed file record).  A
  required-on-pass artifact with no observed file is reported as a gap and is
  never given a fabricated digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_MAX_CAPTURE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ObservedFile:
    """Bytes observed at one container path (real, bounded, deterministic)."""

    path: str
    size: int
    sha256: str


def observed_file(path: str, contents: bytes) -> ObservedFile:
    """One real byte record for a container file or captured stream."""
    return ObservedFile(
        path=path,
        size=len(contents),
        sha256=hashlib.sha256(contents).hexdigest(),
    )


def bounded_capture_bytes(output: str, *, max_bytes: int = _MAX_CAPTURE_BYTES) -> bytes:
    """Deterministic bounded tail of a rail's observed text output."""
    if max_bytes <= 0:
        return b""
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded
    return encoded[-max_bytes:]


# Declared requiredOnPass output artifacts -> the real container file the
# repository runner/adapter writes.  Paths relative to {reports} unless they
# start with / (absolute container path, e.g. a dashboard workdir file).
# Producers verified against in-tree code:
#   python-suite:      profile_rails passes --coverage-data/--coverage-json/
#                      --pytest-phase-report into {reports} (argv builder in
#                      test_support/.../code_quality/profile_rails.py).
#   ambient e2e rail:  writes {reports}/ambient-role-chat-e2e/summary.json
#                      (published artifact; scripts/e2e_harness run).
#   codex probe rail:  env AR_CODEX_PROBE_REPORT={reports}/codex-probe.json
#                      (profile rail command).
#   dashboard suite:   vitest --coverage json reporter writes
#                      dashboard/coverage/coverage-final.json (vitest.config.ts
#                      coverage.reporter includes "json", default file name).
ARTIFACT_FILE_PATHS: dict[str, str] = {
    "python-coverage-data": "coverage.data",
    "python-coverage-json": "coverage.json",
    "python-suite-result": "pytest-phases.json",
    "ambient-role-summary": "ambient-role-chat-e2e/summary.json",
    "codex-probe-result": "codex-probe.json",
    "dashboard-coverage-data": "/workspace/dashboard/coverage/coverage-final.json",
    "dashboard-suite-result": "dashboard-suite-result.json",
}


def build_evidence_bindings(
    evidence_ids: Sequence[str],
    observed_text: str,
    *,
    reference: str,
    max_bytes: int = _MAX_CAPTURE_BYTES,
) -> list[dict[str, object]]:
    """Bind every declared evidence id to the rail's real observed capture."""
    bounded = bounded_capture_bytes(observed_text, max_bytes=max_bytes)
    record = observed_file(reference, bounded)
    return [
        {
            "evidenceId": evidence_id,
            "sha256": record.sha256,
            "size": record.size,
            "reference": reference,
        }
        for evidence_id in evidence_ids
    ]


def build_artifact_bindings(
    output_artifacts: Sequence[Mapping[str, object]],
    observed_files: Mapping[str, ObservedFile],
) -> tuple[list[dict[str, object]], list[str]]:
    """Bind output artifacts to real observed files; never invent a digest.

    Returns the bound artifact records and the sorted ids of required-on-pass
    artifacts whose bytes were not observed (gaps the caller must journal).
    """
    bound: list[dict[str, object]] = []
    gaps: list[str] = []
    for declaration in output_artifacts:
        artifact_id = declaration.get("artifactId")
        if not isinstance(artifact_id, str):
            continue
        record = observed_files.get(artifact_id)
        if record is None:
            if declaration.get("requiredOnPass", True) is not False:
                gaps.append(artifact_id)
            continue
        bound.append(
            {
                "artifactId": artifact_id,
                "sha256": record.sha256,
                "size": record.size,
                "evidenceRef": record.path,
            }
        )
    bound.sort(key=lambda item: str(item["artifactId"]))
    return bound, sorted(gaps)


def payload_json(value: object) -> str:
    """Canonical JSON text used when the executor journals the payload."""
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


__all__ = [
    "ARTIFACT_FILE_PATHS",
    "ObservedFile",
    "bounded_capture_bytes",
    "build_artifact_bindings",
    "build_evidence_bindings",
    "observed_file",
    "payload_json",
]
