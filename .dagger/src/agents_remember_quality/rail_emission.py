"""Observe rail evidence without changing the next rail's execution container."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import dagger
from dagger import ReturnType

from agents_remember_quality import rail_bindings
from agents_remember_quality.environment.runtime import capture_environments
from agents_remember_quality.profile_plan import FrozenRail
from agents_remember_quality.profile_results import QualityProgress

RAIL_EVIDENCE_ROOT = "rail-evidence"


@dataclass(frozen=True)
class RailOutputCapture:
    """Observed output and explicit unavailable streams; empty is still observed."""

    text: str
    unavailable: tuple[str, ...]


async def terminal_rail_outcome(
    progress: QualityProgress, rail: FrozenRail, *, reports: str, executed: bool
) -> dict[str, object]:
    """Bind only the actual executed terminal handle and preserve its failure status."""
    code = progress.step_exit_codes.get(rail.identity, 0)
    status = (
        "skipped"
        if rail.identity in progress.skipped
        else "pass"
        if code in rail.execution.get("successExitCodes", [])
        else "fail"
    )
    outcome: dict[str, object] = {"status": status, "exitCode": code}
    if status == "fail":
        outcome.update(
            exitCode=code or 1,
            failureCode=str(progress.failure_details.get(rail.identity, f"rail-exit-{code or 1}")),
        )
    if executed:
        outcome.update(
            await attach_rail_terminal_bindings(
                progress, rail, reports=reports, include_artifacts=status != "skipped"
            )
        )
    gaps = outcome.get("unboundRequiredArtifacts")
    missing_known = isinstance(gaps, list) and any(
        isinstance(artifact, str)
        and (
            artifact in rail_bindings.ARTIFACT_FILE_PATHS
            or artifact in {item["definition"]["artifactId"] for item in rail.environments}
        )
        for artifact in gaps
    )
    if (outcome.get("unavailableEvidenceStreams") or missing_known) and status == "pass":
        outcome.update(status="fail", exitCode=66, failureCode="rail-evidence-unavailable")
        progress.step_exit_codes[rail.identity] = 66
        progress.exit_code = 66
        progress.completed.remove(rail.identity)
        progress.failure_details[rail.identity] = (
            "required rail artifact or output capture is unavailable"
        )
    return outcome


async def attach_rail_terminal_bindings(
    progress: QualityProgress, rail: FrozenRail, *, reports: str, include_artifacts: bool = True
) -> dict[str, object]:
    """Retain actual bytes/files separately from the shared execution handle."""
    captured = await capture_rail_output(progress.container)
    relative = f"{RAIL_EVIDENCE_ROOT}/{rail.identity_key}.log"
    evidence_ids = tuple(
        str(item["evidenceId"])
        for item in rail.evidence_contract
        if isinstance(item.get("evidenceId"), str)
    )
    evidence: list[dict[str, object]] = []
    if evidence_ids:
        maximum = min(
            rail_bindings.MAX_CAPTURE_BYTES,
            *(int(item["maxBytes"]) for item in rail.evidence_contract),
        )
        retained = rail_bindings.bounded_capture_bytes(captured.text, max_bytes=maximum)
        progress.retained_captures[relative] = retained
        if not captured.unavailable:
            evidence = rail_bindings.build_evidence_bindings(
                evidence_ids, retained, reference=relative
            )
    output_artifacts = rail.output_artifacts if include_artifacts else ()
    observed = await observed_artifact_files(progress.container, reports, output_artifacts)
    for artifact_id, record in observed.items():
        source = rail_bindings.artifact_source_path(artifact_id, reports=reports)
        progress.retained_files[record.path] = progress.container.file(source)
    if include_artifacts:
        observed.update(await capture_environments(progress, rail, reports=reports))
    artifacts, gaps = rail_bindings.build_artifact_bindings(output_artifacts, observed)
    bindings: dict[str, object] = {"evidence": evidence, "artifacts": artifacts}
    if gaps:
        bindings["unboundRequiredArtifacts"] = gaps
    if captured.unavailable and evidence_ids:
        bindings["unavailableEvidenceStreams"] = list(captured.unavailable)
    return bindings


async def capture_rail_output(container: dagger.Container) -> RailOutputCapture:
    """Read both streams before any detached artifact-inspection execution."""
    parts: list[str] = []
    unavailable: list[str] = []
    for stream_name in ("stdout", "stderr"):
        try:
            value = await getattr(container, stream_name)()
        except (dagger.DaggerError, OSError):
            unavailable.append(stream_name)
            continue
        if not isinstance(value, str):
            unavailable.append(stream_name)
        elif value:
            parts.append(value)
    return RailOutputCapture("\n".join(parts), tuple(unavailable))


async def observed_artifact_files(
    container: dagger.Container, reports: str, output_artifacts: Sequence[Mapping[str, object]]
) -> dict[str, rail_bindings.ObservedFile]:
    """Observe regular producer files; retain report-relative semantic locators."""
    observed: dict[str, rail_bindings.ObservedFile] = {}
    for declaration in output_artifacts:
        artifact_id = declaration.get("artifactId")
        if not isinstance(artifact_id, str):
            continue
        relative = rail_bindings.ARTIFACT_FILE_PATHS.get(artifact_id)
        if relative is None:
            continue
        path = rail_bindings.artifact_source_path(artifact_id, reports=reports)
        record = await _observed_file(container, path=path, relative=relative)
        if record is not None:
            observed[artifact_id] = record
    return observed


async def _observed_file(
    container: dagger.Container, *, path: str, relative: str
) -> rail_bindings.ObservedFile | None:
    try:
        exists = await container.exists(
            path, expected_type=dagger.ExistsType.REGULAR_TYPE, do_not_follow_symlinks=True
        )
        if not exists:
            return None
        size = await container.file(path).size()
        handle = await container.with_exec(["sha256sum", path], expect=ReturnType.ANY).sync()
        output = await handle.stdout()
    except (dagger.DaggerError, OSError):
        return None
    digest = output.strip().split()[0] if isinstance(output, str) and output.strip() else ""
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return rail_bindings.ObservedFile(path=relative, size=int(size), sha256=digest)
