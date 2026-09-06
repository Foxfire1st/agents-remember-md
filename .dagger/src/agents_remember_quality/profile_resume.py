"""Reopen retained report bytes before the selected suffix starts any rail."""

from __future__ import annotations

import dagger
from dagger import ReturnType

from agents_remember_quality import rail_bindings
from agents_remember_quality.profile_plan import FrozenProfilePlan

_TRANSPORT_ROOT = "/tmp/ar-retained-reports"
_VERIFY_BATCH_FILES = 128
# Arguments are data, including spaces/newlines in canonical relative file names.
# Walk every component before hashing so transported symlinks never become authority.
_VERIFY_FILES = r"""
root=$1
shift
while [ "$#" -gt 0 ]; do
    relative=$1
    expected_sha=$2
    expected_size=$3
    shift 3
    current=$root
    rest=$relative
    while :; do
        part=${rest%%/*}
        current=$current/$part
        test ! -L "$current" || exit 66
        if [ "$rest" = "$part" ]; then break; fi
        test -d "$current" || exit 66
        rest=${rest#*/}
    done
    test -f "$current" || exit 66
    test "$(stat -c %s -- "$current")" = "$expected_size" || exit 66
    actual=$(sha256sum < "$current")
    test "${actual%% *}" = "$expected_sha" || exit 66
done
"""


async def seed_retained_reports(
    container: dagger.Container,
    plan: FrozenProfilePlan,
    retained_reports: dagger.Directory | None,
    *,
    reports: str,
) -> dagger.Container:
    """Verify selected inputs on a detached inspection branch, then project only them.

    Inspection cannot erase an environment failure or become a fresh rail terminal
    observation. The original publication/result/certificate remain in the gate catalog.
    """
    resume = plan.resume
    needs_transport = resume is not None and resume.first_gate > 1
    if needs_transport != (retained_reports is not None):
        raise ValueError("retained directory presence differs from the selected execution suffix")
    if not needs_transport:
        return container
    if not isinstance(plan.executor.get("retainedReportsArgument"), str):
        raise ValueError("selected executor does not declare retained report transport")
    if resume is None or retained_reports is None:
        raise ValueError("selected suffix lacks its retained execution transport")
    if await container.exit_code() != 0:
        return container
    inspection = container.with_directory(_TRANSPORT_ROOT, retained_reports)
    for offset in range(0, len(resume.files), _VERIFY_BATCH_FILES):
        arguments = ["sh", "-eu", "-c", _VERIFY_FILES, "verify-retained-reports", _TRANSPORT_ROOT]
        for item in resume.files[offset : offset + _VERIFY_BATCH_FILES]:
            arguments.extend((item.path, item.sha256, str(item.size)))
        inspected = await inspection.with_exec(arguments, expect=ReturnType.ANY).sync()
        if await inspected.exit_code() != 0:
            raise ValueError(
                "retained report transport is missing, unsafe, corrupt, or from another generation"
            )
    output = container
    for item in resume.files:
        output = output.with_file(f"{reports}/{item.path}", retained_reports.file(item.path))
    return _restore_producer_paths(output, plan, retained_reports, reports=reports)


def _restore_producer_paths(output, plan, retained_reports, *, reports):
    resume = plan.resume
    if resume is None:
        raise ValueError("producer restoration requires a selected suffix")
    # Rehydrate the existing producer location where a later rail consumes it. The
    # canonical artifact owner alone defines these aliases (e.g. Vitest coverage).
    for row in resume.retained:
        for rail in row["result"]["railResults"]:
            for artifact in rail["artifacts"]:
                artifact_id = artifact["artifactId"]
                relative = artifact["evidenceRef"]
                declared = rail_bindings.ARTIFACT_FILE_PATHS.get(artifact_id)
                if declared is None:
                    continue
                if declared != relative:
                    raise ValueError(
                        "retained artifact locator differs from its declared producer path"
                    )
                target = rail_bindings.artifact_source_path(artifact_id, reports=reports)
                if target != f"{reports}/{relative}":
                    output = output.with_file(target, retained_reports.file(relative))
    return output
