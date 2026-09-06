"""Interpret one frozen gate suffix with exhaustive same-gate terminal records."""

from __future__ import annotations

import json
from dataclasses import dataclass

import dagger
from dagger import ReturnType

from agents_remember_quality import profile_results, rail_emission
from agents_remember_quality.applicability.runtime import (
    not_applicable_outcome,
    publish_source_decisions,
)
from agents_remember_quality.engine_helpers import (
    _first_gate_failure_code,
    _selector_result_failure,
    _selector_result_path,
    _workspace_path,
)
from agents_remember_quality.environment.runtime import reconstruct_environments
from agents_remember_quality.profile_plan import FrozenProfilePlan, FrozenSelector, expand_command
from agents_remember_quality.profile_resume import seed_retained_reports
from agents_remember_quality.selection_result import (
    SelectionAdmission,
    SelectionResultError,
    parse_selection_result,
)

_QualityProgress = profile_results.QualityProgress
_gate_catalog_payload = profile_results.gate_catalog_payload
_GateOutcomes = profile_results.GateOutcomes
MAX_SELECTOR_RESULT_BYTES = 8 * 1024 * 1024
RADON_CHANGED_FILE_RAILS = frozenset({"radon-cc", "radon-mi"})


@dataclass(frozen=True)
class _ProfileRunInputs:
    plan: FrozenProfilePlan
    mode: str
    diff_base: str
    reports: str
    memory_cap_bytes: int
    retained_reports: dagger.Directory | None = None


async def _prepare_profile_environment(
    container: dagger.Container, inputs: _ProfileRunInputs
) -> _QualityProgress:
    container = await seed_retained_reports(
        container, inputs.plan, inputs.retained_reports, reports=inputs.reports
    )
    environment_exit = await container.exit_code()
    progress = _QualityProgress(
        container=container,
        exit_code=environment_exit,
        attempted=["environment"],
        completed=["environment"] if environment_exit == 0 else [],
        step_exit_codes={"environment": environment_exit},
    )
    if environment_exit == 0:
        await reconstruct_environments(progress, inputs.plan, reports=inputs.reports)
        environment_exit = progress.exit_code
    return progress


async def _run_profile_acceptance(
    container: dagger.Container,
    inputs: _ProfileRunInputs,
) -> _QualityProgress:
    """Execute the admitted five-gate profile: exhaustive within, fail-fast between.

    Every independent applicable sibling rail inside a started gate runs to a terminal
    pass/skipped/fail/blocked result; a failed same-gate prerequisite blocks only its
    dependants. The aggregate gate verdict is published with the complete rail catalog
    and a red gate stops exactly the next gate -- later gates record zero starts and
    never begin a command.
    """
    progress = await _prepare_profile_environment(container, inputs)
    environment_exit = progress.exit_code
    scalar_values = {
        "reports": inputs.reports,
        "selection-mode": inputs.mode,
        "candidate-kind": inputs.plan.candidate_kind,
        "candidate-value": inputs.plan.candidate_value,
        "diff-base": inputs.diff_base,
        "memory-cap-bytes": str(inputs.memory_cap_bytes),
        "clean-room": "/workspace",
    }
    scalar_values.update(publish_source_decisions(progress, inputs.plan, reports=inputs.reports))
    selector_results: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {}
    executed_selectors: set[str] = set()
    gates_catalog: list[dict[str, object]] = []
    gate_failure_code: int | None = None
    for gate_plan in inputs.plan.gate_plans:
        resume = inputs.plan.resume
        if resume is not None and gate_plan.gate < resume.first_gate:
            gates_catalog.append(resume.reused_catalog(gate_plan.gate))
            continue
        if gate_failure_code is not None:
            gates_catalog.append(
                _gate_catalog_payload(
                    gate_plan.gate,
                    _GateOutcomes(
                        applicability=gate_plan.applicability,
                        started=False,
                        disposition="not-run",
                        rails=gate_plan.rails,
                        rail_outcomes={},
                        selector_outcomes={},
                    ),
                )
            )
            continue
        if gate_plan.applicability != "applicable":
            gates_catalog.append(
                _gate_catalog_payload(
                    gate_plan.gate,
                    _GateOutcomes(
                        applicability=gate_plan.applicability,
                        started=False,
                        disposition="not-applicable",
                        rails=(),
                        rail_outcomes={},
                        selector_outcomes={},
                    ),
                )
            )
            continue
        if environment_exit != 0:
            gates_catalog.append(
                _gate_catalog_payload(
                    gate_plan.gate,
                    _GateOutcomes(
                        applicability=gate_plan.applicability,
                        started=False,
                        disposition="not-run",
                        rails=gate_plan.rails,
                        rail_outcomes={},
                        selector_outcomes={},
                    ),
                )
            )
            continue
        selector_outcomes: dict[str, dict[str, object]] = {}
        for selector in gate_plan.selectors:
            name = f"selector:{selector.selector_id}"
            if selector.selector_id in executed_selectors:
                selector_outcomes[selector.selector_id] = {
                    "status": "reused",
                    "exitCode": 0,
                }
                continue
            result_path, values = await _run_profile_selector(
                progress,
                selector,
                scalar_values=scalar_values,
            )
            executed_selectors.add(selector.selector_id)
            code = progress.step_exit_codes.get(name, 0)
            if code != 0:
                selector_outcomes[selector.selector_id] = {
                    "status": "fail",
                    "exitCode": code,
                    "failureCode": str(progress.failure_details.get(name, "selector-failed")),
                }
            else:
                selector_results[selector.selector_id] = (result_path, values)
                selector_outcomes[selector.selector_id] = {"status": "pass", "exitCode": 0}
        rail_outcomes = await _execute_gate_rails(
            progress,
            gate_plan,
            scalar_values=scalar_values,
            selector_results=selector_results,
            selector_outcomes=selector_outcomes,
        )
        red = any(
            outcome["status"] in {"fail", "blocked"} for outcome in rail_outcomes.values()
        ) or any(outcome["status"] == "fail" for outcome in selector_outcomes.values())
        disposition = "red" if red else "green"
        gates_catalog.append(
            _gate_catalog_payload(
                gate_plan.gate,
                _GateOutcomes(
                    applicability=gate_plan.applicability,
                    started=True,
                    disposition=disposition,
                    rails=gate_plan.rails,
                    rail_outcomes=rail_outcomes,
                    selector_outcomes=selector_outcomes,
                ),
            )
        )
        if red:
            gate_failure_code = _first_gate_failure_code(progress, gate_plan, rail_outcomes)
            progress.exit_code = gate_failure_code or progress.exit_code
    progress.gate_catalog = gates_catalog
    return progress


def _rail_scope_lists(rail, rail_lists: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """Radon rails receive changed python (lint) when product coverage is empty.

    coverage-paths stays exactly product-derived (the rail-scope equality
    the suite checks); the radon report-only rails fall back to the changed
    python files in their own command scope so they are never invoked with an
    empty path set.
    """
    if rail.identity not in RADON_CHANGED_FILE_RAILS or rail_lists.get("coverage-paths"):
        return rail_lists
    return {**rail_lists, "coverage-paths": rail_lists.get("lint-paths", ())}


async def _execute_gate_rails(
    progress: _QualityProgress,
    gate_plan,
    *,
    scalar_values: dict[str, str],
    selector_results: dict[str, tuple[str, dict[str, tuple[str, ...]]]],
    selector_outcomes: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Terminalize every runnable sibling rail; failed prerequisites block dependants."""
    rail_outcomes: dict[str, dict[str, object]] = {}
    failed_same_gate: set[str] = set()
    for rail in gate_plan.rails:
        if rail.applicability["status"] == "not-applicable":
            rail_outcomes[rail.identity_key] = not_applicable_outcome(rail)
            progress.not_applicable[rail.identity] = rail.source_selection["declaration"][
                "evidencePath"
            ]
            continue
        execution = rail.execution
        if execution.get("adapterKind") != "container-command":
            raise ValueError(
                f"rail {rail.identity_key} names unsupported sandbox adapter "
                f"{execution.get('adapterKind')!r}"
            )
        blockers = [
            key
            for key in rail.prerequisites
            if key in gate_plan.rail_keys and key in failed_same_gate
        ]
        blocked_step = f"blocked:{rail.identity}"
        if blockers:
            progress.attempted.append(blocked_step)
            progress.step_exit_codes[blocked_step] = 125
            progress.failure_details[blocked_step] = "same-gate prerequisite failed: " + ", ".join(
                blockers
            )
            rail_outcomes[rail.identity_key] = {
                "status": "blocked",
                "exitCode": None,
                "blockedBy": list(blockers),
                "failureCode": "same-gate-prerequisite-failed",
            }
            continue
        provider = execution.get("scopeProviderId")
        if provider is not None:
            selector_status = selector_outcomes.get(provider, {}).get("status")
            if selector_status == "fail":
                progress.attempted.append(blocked_step)
                progress.step_exit_codes[blocked_step] = 125
                progress.failure_details[blocked_step] = f"selector {provider} failed"
                rail_outcomes[rail.identity_key] = {
                    "status": "blocked",
                    "exitCode": None,
                    "blockedBy": [provider],
                    "failureCode": "selector-failed",
                }
                continue
            if not isinstance(provider, str) or provider not in selector_results:
                raise ValueError(f"rail {rail.identity_key} lacks its admitted selector result")
        rail_scalars = dict(scalar_values)
        rail_lists: dict[str, tuple[str, ...]] = {}
        if provider is not None:
            selector_path, rail_lists = selector_results[provider]
            rail_scalars["selector-output"] = selector_path
        rail_lists = _rail_scope_lists(rail, rail_lists)
        command = expand_command(
            execution.get("command", []),
            scalar_values=rail_scalars,
            list_values=rail_lists,
        )
        executed = await _run_profile_rail(progress, rail.identity, execution, command)
        outcome = await rail_emission.terminal_rail_outcome(
            progress, rail, reports=scalar_values["reports"], executed=executed
        )
        rail_outcomes[rail.identity_key] = outcome
        if outcome["status"] == "fail":
            failed_same_gate.add(rail.identity_key)
    return rail_outcomes


async def _run_profile_selector(
    progress: _QualityProgress,
    selector: FrozenSelector,
    *,
    scalar_values: dict[str, str],
) -> tuple[str, dict[str, tuple[str, ...]]]:
    definition = selector.definition
    result_path = _selector_result_path(str(definition["resultPath"]))
    command = expand_command(
        definition.get("command", []),
        scalar_values={
            **scalar_values,
            "selector-output": result_path,
            "selector-id": selector.selector_id,
            "selector-version": str(definition["version"]),
            "selector-configuration-digest": str(definition["configurationDigest"]),
        },
        list_values={},
    )
    name = f"selector:{selector.selector_id}"
    progress.attempted.append(name)
    progress.container = (
        await progress.container.with_workdir(
            _workspace_path(str(definition["workingDirectory"]), directory=True)
        )
        .with_exec(command, expect=ReturnType.ANY)
        .sync()
    )
    code = await progress.container.exit_code()
    progress.step_exit_codes[name] = code
    progress.exit_code = code
    if code != 0:
        return result_path, {}
    exists = await progress.container.exists(
        result_path,
        expected_type=dagger.ExistsType.REGULAR_TYPE,
        do_not_follow_symlinks=True,
    )
    size = await progress.container.file(result_path).size() if exists else None
    if size is None or size > MAX_SELECTOR_RESULT_BYTES:
        return _selector_result_failure(
            progress,
            name,
            result_path,
            "selector result is missing, unsafe, or exceeds the framework maximum",
        )
    try:
        payload = json.loads(await progress.container.file(result_path).contents())
    except (json.JSONDecodeError, TypeError):
        return _selector_result_failure(
            progress,
            name,
            result_path,
            "selector result is not valid JSON",
        )
    outputs = definition.get("outputArtifacts")
    if not isinstance(outputs, list) or any(not isinstance(item, str) for item in outputs):
        raise ValueError(f"selector {selector.selector_id} output contract is invalid")
    try:
        parsed = parse_selection_result(
            payload,
            admission=SelectionAdmission(
                selector_id=selector.selector_id,
                selector_version=str(definition["version"]),
                configuration_digest=str(definition["configurationDigest"]),
                candidate_kind=scalar_values["candidate-kind"],
                candidate_value=scalar_values["candidate-value"],
                mode=scalar_values["selection-mode"],
                base_revision=scalar_values["diff-base"],
            ),
            output_artifacts=tuple(outputs),
        )
    except SelectionResultError as error:
        if str(error).startswith("test-selection-ownership-incomplete:"):
            progress.selection_results[selector.selector_id] = payload
        return _selector_result_failure(
            progress,
            name,
            result_path,
            str(error),
        )
    progress.selection_results[selector.selector_id] = payload
    progress.completed.append(name)
    return result_path, parsed.values


async def _run_profile_rail(
    progress: _QualityProgress,
    name: str,
    execution: dict[str, object],
    command: list[str],
) -> bool:
    success_codes = execution.get("successExitCodes")
    skipped_codes = execution.get("skippedExitCodes")
    if (
        not isinstance(success_codes, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in success_codes)
        or not isinstance(skipped_codes, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in skipped_codes)
    ):
        raise ValueError(f"rail {name} exit contract is invalid")
    environment = execution.get("environmentContract")
    if not isinstance(environment, list) or any(
        not isinstance(item, str) or not item for item in environment
    ):
        raise ValueError(f"rail {name} environment contract is invalid")
    missing_environment = [
        item for item in environment if await progress.container.env_variable(item) is None
    ]
    if missing_environment:
        progress.attempted.append(name)
        progress.exit_code = 125
        progress.step_exit_codes[name] = progress.exit_code
        progress.failure_details[name] = (
            "executor prerequisite missing declared environment: " + ", ".join(missing_environment)
        )
        return False
    timeout_seconds = execution.get("timeoutSeconds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValueError(f"rail {name} timeout contract is invalid")
    progress.attempted.append(name)
    bounded_command = [
        "timeout",
        "--signal=TERM",
        "--kill-after=10s",
        f"{timeout_seconds}s",
        *command,
    ]
    progress.container = (
        await progress.container.with_workdir(
            _workspace_path(str(execution["workingDirectory"]), directory=True)
        )
        .with_exec(bounded_command, expect=ReturnType.ANY)
        .sync()
    )
    code = await progress.container.exit_code()
    progress.step_exit_codes[name] = code
    if code in skipped_codes:
        progress.skipped.append(name)
        progress.exit_code = 0
    elif code in success_codes:
        progress.completed.append(name)
        progress.exit_code = 0
    else:
        progress.exit_code = code or 1
    return True
