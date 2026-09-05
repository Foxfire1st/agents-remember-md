"""Container-side per-rail emission that attaches real bindings to outcomes.

Kept out of main.py so the executor module stays under the file-size limit.
The bindings themselves are built by :mod:; this module only
observes the rail's real container output and real artifact files and hands
them to the binding builders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dagger import ReturnType

from agents_remember_quality import rail_bindings

RAIL_EVIDENCE_ROOT = "/tmp/ar-quality/rail-evidence"


async def attach_rail_terminal_bindings(
    progress: Any,
    rail: Any,
    *,
    reports: str,
) -> dict[str, object]:
    """Attach real per-rail evidence + artifact bindings to a passed rail.

    Evidence is the bounded capture of the rail's own stdout/stderr read from
    the container; the reference names the canonical executor evidence-log path
    under RAIL_EVIDENCE_ROOT.  Artifacts bind only files the repository
    runner verifiably writes into the exported reports directory; any
    required-on-pass artifact without an observed file is journaled as a gap,
    never fabricated.

    This function NEVER mutates the execution container: it never reassigns
    progress.container and every inspection exec runs on a detached handle,
    so the container the next rail executes on is byte-identical to the one
    this rail finished on.
    """
    captured = await capture_rail_output(progress.container)
    log_path = f"{RAIL_EVIDENCE_ROOT}/{rail.identity_key}.log"
    evidence_ids = tuple(
        str(item["evidenceId"])
        for item in rail.evidence_contract
        if isinstance(item.get("evidenceId"), str)
    )
    evidence = rail_bindings.build_evidence_bindings(
        evidence_ids,
        captured,
        reference=log_path,
    )
    artifacts, gaps = rail_bindings.build_artifact_bindings(
        rail.output_artifacts,
        await observed_artifact_files(progress.container, reports, rail.output_artifacts),
    )
    bindings: dict[str, object] = {
        "evidence": evidence,
        "artifacts": artifacts,
    }
    if gaps:
        bindings["unboundRequiredArtifacts"] = gaps
    return bindings


async def capture_rail_output(container: Any) -> str:
    """Read the bounded stdout+stderr of the rail's last exec, if exposed."""
    parts: list[str] = []
    for stream_name in ("stdout", "stderr"):
        try:
            value = await getattr(container, stream_name)()
        except Exception:
            continue
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


async def observed_artifact_files(
    container: Any,
    reports: str,
    output_artifacts: Sequence[Mapping[str, object]],
) -> dict[str, rail_bindings.ObservedFile]:
    """Digest the repository-produced artifact files that actually exist.

    File sizes are read with the read-only file API and digests are computed on
    a detached exec handle; the shared container is never extended.
    """
    observed: dict[str, rail_bindings.ObservedFile] = {}
    for declaration in output_artifacts:
        artifact_id = declaration.get("artifactId")
        artifact_key = artifact_id if isinstance(artifact_id, str) else None
        file_name = (
            rail_bindings.ARTIFACT_FILE_PATHS.get(artifact_key)
            if artifact_key is not None
            else None
        )
        if file_name is None or artifact_key is None:
            continue
        path = file_name if file_name.startswith("/") else f"{reports}/{file_name}"
        try:
            size = await container.file(path).size()
        except Exception:
            continue
        digest_handle = await container.with_exec(["sha256sum", path], expect=ReturnType.ANY).sync()
        digest = await digest_handle.stdout()
        digest_text = (
            digest.strip().split()[0] if isinstance(digest, str) and digest.strip() else None
        )
        if digest_text is None or len(digest_text) != 64:
            continue
        observed[artifact_key] = rail_bindings.ObservedFile(
            path=path,
            size=int(size),
            sha256=digest_text,
        )
    return observed


__all__ = [
    "RAIL_EVIDENCE_ROOT",
    "attach_rail_terminal_bindings",
    "capture_rail_output",
    "observed_artifact_files",
]
