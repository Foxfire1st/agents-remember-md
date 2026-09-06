"""Record original dependency censuses and verify explicit suffix reconstruction."""

from __future__ import annotations

import hashlib
import json

import dagger
from dagger import ReturnType

from agents_remember_quality.engine_helpers import _workspace_path
from agents_remember_quality.environment.census import digest
from agents_remember_quality.profile_plan import FrozenProfilePlan, FrozenRail
from agents_remember_quality.profile_results import QualityProgress
from agents_remember_quality.rail_bindings import ObservedFile

_CENSUS_SCRIPT = "/workspace/.dagger/src/agents_remember_quality/environment/census.py"


def _inspection(container, request, output, original=None):
    definition = request["definition"]
    request_path = f"/tmp/ar-profile/environment-{definition['environmentId']}.json"
    command = [
        definition["inspectionExecutable"],
        _CENSUS_SCRIPT,
        request_path,
        "/workspace",
        output,
    ]
    if original is not None:
        command.append(original)
    return container.with_new_file(request_path, contents=json.dumps(request)).with_exec(
        [
            "timeout",
            "--signal=TERM",
            "--kill-after=10s",
            str(definition["timeoutSeconds"]) + "s",
            *command,
        ],
        expect=ReturnType.ANY,
    )


async def capture_environments(
    progress: QualityProgress,
    rail: FrozenRail,
    *,
    reports: str,
) -> dict[str, ObservedFile]:
    observed = {}
    for request in rail.environments:
        definition = request["definition"]
        path = f"{reports}/{definition['manifestPath']}"
        inspection = await _inspection(progress.container, request, path).sync()
        if await inspection.exit_code() != 0:
            continue
        exists = await inspection.exists(
            path, expected_type=dagger.ExistsType.REGULAR_TYPE, do_not_follow_symlinks=True
        )
        if not exists:
            continue
        size = await inspection.file(path).size()
        if size > definition["maxManifestBytes"]:
            continue
        raw = await inspection.file(path).contents()
        import_digest = hashlib.sha256(raw.encode()).hexdigest()
        record = ObservedFile(definition["manifestPath"], size, import_digest)
        observed[definition["artifactId"]] = record
        progress.retained_files[record.path] = inspection.file(path)
    return observed


async def reconstruct_environments(
    progress: QualityProgress,
    plan: FrozenProfilePlan,
    *,
    reports: str,
) -> None:
    resume = plan.resume
    if resume is None or resume.first_gate == 1:
        return
    for rail in plan.rails:
        for request in rail.environments:
            definition = request["definition"]
            if not any(gate >= resume.first_gate for gate in definition["consumingGates"]):
                continue
            await _reconstruct_one(progress, request, reports=reports)
            if progress.exit_code != 0:
                return


async def _reconstruct_one(progress: QualityProgress, request: dict, *, reports: str) -> None:
    definition = request["definition"]
    original = f"{reports}/{definition['manifestPath']}"
    if not await progress.container.exists(
        original, expected_type=dagger.ExistsType.REGULAR_TYPE, do_not_follow_symlinks=True
    ):
        raise ValueError("selected suffix lacks its original certified environment census")
    if await progress.container.file(original).size() > definition["maxManifestBytes"]:
        raise ValueError("original environment census exceeds its admitted byte bound")
    payload = json.loads(await progress.container.file(original).contents())
    _require_original(payload, request)
    step = f"environment-reconstruction:{definition['environmentId']}"
    progress.attempted.append(step)
    progress.environment_reconstruction.append(
        {
            "environmentId": definition["environmentId"],
            "declarationDigest": digest(definition),
            "originalCensusDigest": payload["censusDigest"],
            "status": "started",
            "command": list(definition["command"]),
            "workingDirectory": definition["workingDirectory"],
            "proof": definition["reconstructionProofPath"],
        }
    )
    progress.container = (
        await progress.container.with_workdir(
            _workspace_path(definition["workingDirectory"], directory=True)
        )
        .with_exec(
            [
                "timeout",
                "--signal=TERM",
                "--kill-after=10s",
                str(definition["timeoutSeconds"]) + "s",
                *definition["command"],
            ],
            expect=ReturnType.ANY,
        )
        .sync()
    )
    code = await progress.container.exit_code()
    if code == 0:
        proof_path = f"{reports}/{definition['reconstructionProofPath']}"
        progress.container = await _inspection(
            progress.container, request, proof_path, original
        ).sync()
        code = await progress.container.exit_code()
        if code == 0:
            code = await _retain_reconstruction_proof(progress, definition, proof_path, payload)
    progress.environment_reconstruction[-1].update(
        status="verified" if code == 0 else "failed", exitCode=code
    )
    progress.step_exit_codes[step] = code
    if code == 0:
        progress.completed.append(step)
    else:
        progress.failure_details[step] = (
            "declared environment reconstruction or exact original census comparison failed"
        )
        progress.exit_code = code


async def _retain_reconstruction_proof(progress, definition, proof_path, original):
    if (
        not await progress.container.exists(
            proof_path, expected_type=dagger.ExistsType.REGULAR_TYPE, do_not_follow_symlinks=True
        )
        or await progress.container.file(proof_path).size() > 4096
    ):
        return 66
    proof = json.loads(await progress.container.file(proof_path).contents())
    if proof != {
        "schemaVersion": "certification-environment-reconstruction/v1",
        "status": "verified",
        "censusDigest": original["censusDigest"],
        "declarationDigest": original["declarationDigest"],
    }:
        return 66
    progress.retained_files[definition["reconstructionProofPath"]] = progress.container.file(
        proof_path
    )
    return 0


def _require_original(payload: object, request: dict) -> None:
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != "certification-environment-census/v1"
    ):
        raise ValueError("original environment census schema is invalid")
    if payload.get("censusDigest") != digest(
        {key: value for key, value in payload.items() if key != "censusDigest"}
    ):
        raise ValueError("original environment census digest differs")
    if (
        payload.get("candidateIdentity"),
        payload.get("declarationDigest"),
        payload.get("runtimeDigest"),
    ) != (request["candidateIdentity"], digest(request["definition"]), request["runtimeDigest"]):
        raise ValueError(
            "original environment census belongs to another candidate, declaration or runtime"
        )
