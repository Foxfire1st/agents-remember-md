"""Small engine helper primitives shared by the pinned quality module."""

from __future__ import annotations

import os

import dagger

from agents_remember_quality import profile_results
from agents_remember_quality.profile_plan import (
    FrozenProfilePlan,
    load_execution_manifest,
    module_runtime_digest,
    pinned_image_digest,
)

_QualityProgress = profile_results.QualityProgress


async def _admitted_profile_plan(
    execution_manifest: dagger.File,
    *,
    function_name: str,
) -> FrozenProfilePlan:
    plan = load_execution_manifest(await execution_manifest.contents())
    if plan.executor.get("runtimeDigest") != module_runtime_digest():
        raise ValueError("execution manifest names another Dagger adapter runtime")
    if plan.executor.get("functionName") != function_name:
        raise ValueError("execution manifest names another Dagger adapter function")
    return plan


def _require_plan_authority(plan: FrozenProfilePlan) -> str:
    """Require the frozen host-authority digest bound into the admission manifest.

    A certifying Dagger run without the admitted shared runner authority digest fails
    closed with a typed message instead of silently launching on ambient state.
    """
    digest = plan.runtime_authority_digest
    ambient = os.environ.get("AR_DAGGER_RUNTIME_AUTHORITY_DIGEST")
    if digest is None:
        raise ValueError(
            "dagger-runtime-authority-missing: the execution manifest carries no admitted "
            "shared runner/layer-store authority"
        )
    if ambient is not None and ambient.strip() and ambient.strip() != digest:
        raise ValueError(
            "dagger-runtime-authority-mismatch: ambient authority digest differs from the "
            "admitted execution manifest"
        )
    return digest


def _require_adapter_runtime(plan: FrozenProfilePlan) -> str:
    """Require every rail to consume the admitted single-image adapter runtime."""

    image_reference = str(plan.executor["imageReference"])
    image_digest = pinned_image_digest(image_reference)
    mismatched = sorted(
        rail.identity for rail in plan.rails if rail.runtime.get("imageDigest") != image_digest
    )
    if mismatched:
        raise ValueError(
            "repository profile rails name runtime images outside the admitted adapter: "
            + ", ".join(mismatched)
        )
    return image_reference


def _first_gate_failure_code(
    progress: _QualityProgress,
    gate_plan,
    rail_outcomes: dict[str, dict[str, object]],
) -> int | None:
    """The first failing rail code of the red gate, preserving the exact boundary."""
    for rail in gate_plan.rails:
        outcome = rail_outcomes.get(rail.identity_key)
        if outcome is None:
            continue
        exit_code = outcome.get("exitCode")
        if outcome["status"] == "fail" and isinstance(exit_code, int):
            return exit_code
        if outcome["status"] == "blocked":
            return 125
    for selector in gate_plan.selectors:
        code = progress.step_exit_codes.get(f"selector:{selector.selector_id}", 0)
        if code != 0:
            return code
    return None


def _selector_result_failure(
    progress: _QualityProgress,
    name: str,
    result_path: str,
    detail: str,
) -> tuple[str, dict[str, tuple[str, ...]]]:
    progress.exit_code = 65
    progress.step_exit_codes[name] = progress.exit_code
    progress.failure_details[name] = detail
    return result_path, {}


def _workspace_path(relative: str, *, directory: bool = False) -> str:
    if relative == "." and directory:
        return "/workspace"
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("profile execution path is not confined to the candidate")
    return f"/workspace/{relative}"


def _selector_result_path(relative: str) -> str:
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("profile selector result path is not confined")
    return f"/tmp/ar-profile/{relative}"


async def _verify_required_profile_publications(
    progress: _QualityProgress,
    plan: FrozenProfilePlan,
    *,
    reports: str,
    output: dagger.Container,
) -> None:
    """Consume the admitted publication policies before terminal publication."""

    if progress.exit_code != 0:
        return
    terminal_path = str(plan.decoder["artifactPath"])
    for publication in plan.published_artifacts:
        if publication.get("required") is not True:
            continue
        relative = str(publication["path"])
        if relative == terminal_path:
            continue
        name = f"publication:{relative}"
        progress.attempted.append(name)
        path = f"{reports}/{relative}"
        exists = await output.exists(
            path,
            expected_type=dagger.ExistsType.REGULAR_TYPE,
            do_not_follow_symlinks=True,
        )
        maximum = publication.get("maxBytes")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError(f"publication {relative} has an invalid size contract")
        size = await output.file(path).size() if exists else None
        if size is None or size > maximum:
            progress.exit_code = 66
            progress.step_exit_codes[name] = progress.exit_code
            progress.failure_details[name] = (
                "required publication is missing, unsafe, or exceeds its admitted maximum"
            )
            return
        progress.completed.append(name)
        progress.step_exit_codes[name] = 0
