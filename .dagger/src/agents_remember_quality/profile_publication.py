"""Assemble the exported report branch after all rail execution has stopped."""

from __future__ import annotations

import base64
import json

import dagger

from agents_remember_quality.engine_helpers import (
    _require_plan_authority,
    _verify_required_profile_publications,
)
from agents_remember_quality.profile_plan import FrozenProfilePlan
from agents_remember_quality.profile_results import QualityProgress


async def prepare_profile_reports(
    progress: QualityProgress,
    plan: FrozenProfilePlan,
    *,
    reports: str,
) -> dagger.Container:
    """Persist retained exact bytes and verify the actual exported branch."""
    output = progress.container
    for relative, source_file in sorted(progress.retained_files.items()):
        output = output.with_file(f"{reports}/{relative}", source_file)
    for relative, contents in sorted(progress.retained_captures.items()):
        output = output.with_new_file(
            "/tmp/ar-quality/retained-capture.b64",
            contents=base64.b64encode(contents).decode("ascii"),
        ).with_exec(
            [
                "sh",
                "-c",
                'mkdir -p -- "$1/rail-evidence" && base64 -d "$2" > "$3"',
                "publish-rail-evidence",
                reports,
                "/tmp/ar-quality/retained-capture.b64",
                f"{reports}/{relative}",
            ]
        )
    await _verify_required_profile_publications(progress, plan, reports=reports, output=output)
    return output


def export_profile_reports(
    output: dagger.Container,
    progress: QualityProgress,
    plan: FrozenProfilePlan,
    payload: dict[str, object],
    *,
    reports: str,
) -> dagger.Directory:
    """Serialize the terminal payload after report preparation updates its outcome."""
    payload[plan.decoder["statusField"]] = (
        plan.decoder["passedValue"] if progress.exit_code == 0 else plan.decoder["failedValue"]
    )
    payload[plan.decoder["exitCodeField"]] = progress.exit_code
    payload["profileDigest"] = plan.profile_digest
    payload["profilePlanDigest"] = plan.plan_digest
    payload["runtimeAuthorityDigest"] = _require_plan_authority(plan)
    output = output.with_new_file(
        f"{reports}/{plan.decoder['artifactPath']}",
        contents=json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return output.directory(reports)
