"""CCR-R17-v3 mandatory acceptance scenarios projected to measured outcomes.

Each of the seventeen mandatory acceptance scenarios is a deterministic
function of the measured evidence (a treatment run and, where the scenario is
inherently a comparison, its baseline).  A scenario outcome is green only when
the measured export proves the scenario, red when the export contradicts it,
and not-applicable when the scenario's precondition never occurred.  Outcomes
never carry numeric reduction thresholds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from agents_remember.certification.models import (
    CertificationContractFinding,
    RailIdentity,
)
from agents_remember.certification.replay.models import (
    GateRunMeasurement,
    ReplayProfileSnapshot,
    ReplayRailPlacement,
    ReplayScenarioEvidence,
    ReplayScenarioExpectation,
    RunMeasurement,
    ScenarioOutcome,
    ScenarioState,
)
from agents_remember.errors import CertificationContractError

_SCENARIO_IDS: tuple[str, ...] = tuple(f"r17-scenario-{number:02d}" for number in range(1, 18))

REPLAY_ACCEPTANCE_SCENARIOS: tuple[ReplayScenarioExpectation, ...] = (
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-01",
        title="two independent Gate-1 rails fail and both appear in one complete catalog",
        requirement=(
            "Two independent Gate-1 rails fail and both appear in one complete catalog; "
            "no first-failure-only truncation."
        ),
        views=("Gate-1 red catalog",),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-02",
        title="a failed Gate-1 prerequisite blocks only dependants",
        requirement=(
            "A failed Gate-1 prerequisite blocks only its dependants; unrelated Gate-1 "
            "rails execute."
        ),
        views=("Gate-1 red catalog", "prerequisite closure"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-03",
        title="file size fails with zero Gate-2/3/4/5 starts",
        requirement=(
            "A Gate-1 file-size failure produces zero Gate-2/3/4/5 starts with no "
            "blocked-gate work counted as saved."
        ),
        views=("Gate-1 red catalog", "zero-start evidence"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-04",
        title="pyright fails without hiding file size, layering, or dashboard results",
        requirement=(
            "A Gate-1 pyright failure still publishes file-size, layering, and dashboard "
            "rail results in the same complete catalog."
        ),
        views=("Gate-1 red catalog", "companion rail results"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-05",
        title="Gate-2 suite failure produces zero Gate-3/4/5 starts",
        requirement="A Gate-2 suite failure produces zero Gate-3/4/5 starts.",
        views=("Gate-2 red catalog", "zero-start evidence"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-06",
        title="Gate 3 consumes exact green Gate-2 artifacts and reports offenders",
        requirement=(
            "Gate 3 consumes the exact green Gate-2 artifacts and reports every "
            "CRAP/diff-coverage offender in its catalog."
        ),
        views=("Gate-2 green", "Gate-3 catalog", "offender rails"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-07",
        title="Gate-3 failure produces zero Gate-4/5 starts",
        requirement="A Gate-3 failure produces zero Gate-4/5 starts.",
        views=("Gate-3 red catalog", "zero-start evidence"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-08",
        title="Gate-4 failure produces zero Gate-5 starts",
        requirement="A Gate-4 failure produces zero Gate-5 starts.",
        views=("Gate-4 red catalog", "zero-start evidence"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-09",
        title="memory-only failure and repair reuse exact Gates 1-4",
        requirement=(
            "A memory-only failure and its repair reuse the exact green Gates 1-4 "
            "certificates and re-run only Gate 5."
        ),
        views=("baseline Gate-5 red", "treatment reuse"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-10",
        title="code change invalidates Gates 1-5",
        requirement="A code change invalidates every gate certificate 1-5.",
        views=("code input change", "certificate invalidation closure"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-11",
        title="per-gate profile/config changes invalidate only their declared closure",
        requirement=(
            "A per-gate profile or configuration change invalidates only its declared "
            "gate and downstream closure."
        ),
        views=("declared closure", "observed invalidation"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-12",
        title="review/journal/approval metadata changes invalidate no certificate",
        requirement=(
            "Review, journal, and approval metadata changes invalidate no gate certificate."
        ),
        views=("metadata change", "zero invalidation"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-13",
        title="interrupted finalization resumes with zero unchanged gate starts",
        requirement=(
            "An interrupted finalization resumes with zero unchanged gate starts and "
            "reuses the exact green prefix."
        ),
        views=("finalization resume", "zero-start reuse"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-14",
        title="pre-commit and closeout use identical canonical rail definitions",
        requirement=("Pre-commit and closeout replay use identical canonical rail definitions."),
        views=("canonical rail identity",),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-15",
        title="no legacy, fallback, safe-full, diagnostic, or stale evidence certifies",
        requirement=(
            "No legacy reader, fallback plan, safe-full selection, diagnostic result, "
            "partial/stale artifact, or generic exception can become certifying evidence."
        ),
        views=("closeout-only envelope", "no promotion"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-16",
        title="repository-owned Gate 1-4 profiles share the framework order contract",
        requirement=(
            "Two non-Agents-Remember repository fixtures with different languages, test, "
            "and E2E tools use repository-owned Gate 1-4 profiles and the same framework "
            "order/result contracts."
        ),
        views=("repository profiles", "framework order contract"),
    ),
    ReplayScenarioExpectation(
        scenarioId="r17-scenario-17",
        title="Agents Remember migrated profile preserves rail placements",
        requirement=(
            "The Agents Remember migrated profile preserves every current hard rail and "
            "places pytest in Gate 2, CRAP/diff coverage in Gate 3, real-Codex E2E in "
            "Gate 4, and memory in Gate 5."
        ),
        views=("reference profile", "rail placement parity"),
    ),
)


def evaluate_replay_scenario(
    scenario_id: str,
    evidence: ReplayScenarioEvidence,
) -> ScenarioOutcome:
    """Project one mandatory acceptance scenario over measured replay evidence."""
    evaluator = _EVALUATORS.get(scenario_id)
    if evaluator is None:
        raise CertificationContractError(f"unknown replay acceptance scenario {scenario_id}", [])
    return evaluator(evidence)


def evaluate_all_replay_scenarios(
    evidence: ReplayScenarioEvidence,
) -> tuple[ScenarioOutcome, ...]:
    """Project every mandatory acceptance scenario into ordered machine outcomes."""
    return tuple(evaluate_replay_scenario(scenario_id, evidence) for scenario_id in _SCENARIO_IDS)


def _not_applicable(scenario_id: str, detail: str) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenarioId=scenario_id,
        state="not-applicable",
        findings=(_finding(scenario_id, "not-applicable", detail),),
    )


def _red(scenario_id: str, detail: str) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenarioId=scenario_id,
        state="red",
        findings=(_finding(scenario_id, "scenario-contradicted", detail),),
    )


def _finding(scenario_id: str, code: str, detail: str) -> CertificationContractFinding:
    return CertificationContractFinding(code=code, path=scenario_id, detail=detail)


def _evaluate_01(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    gate_one = evidence.treatment.gates[0]
    if gate_one.lastCatalogDisposition != "red" or not gate_one.lastCatalog:
        return _not_applicable("r17-scenario-01", "no Gate-1 red catalog was recorded")
    failed = gate_one.failedRails
    if len({rail.key for rail in failed}) >= 2:
        return ScenarioOutcome(scenarioId="r17-scenario-01", state="green", findings=())
    return _red(
        "r17-scenario-01",
        f"Gate-1 red catalog holds {len(failed)} failed rails, fewer than the two required",
    )


def _evaluate_02(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    gate_one = evidence.treatment.gates[0]
    if gate_one.lastCatalogDisposition != "red" or not gate_one.lastCatalog:
        return _not_applicable("r17-scenario-02", "no Gate-1 red catalog was recorded")
    failed_keys = {rail.key for rail in gate_one.failedRails}
    blocked = gate_one.blockedRails
    if not failed_keys:
        return _not_applicable("r17-scenario-02", "no Gate-1 rail failed")
    dependant_keys = {
        edge.dependant.key
        for edge in evidence.dependencyEdges
        if edge.prerequisite.key in failed_keys
    }
    unrelated_blocked = [rail.key for rail in blocked if rail.key not in dependant_keys]
    if unrelated_blocked:
        return _red(
            "r17-scenario-02",
            "a blocked Gate-1 rail is not a dependant of any failed prerequisite: "
            + ", ".join(sorted(unrelated_blocked)),
        )
    unrelated_executed = [
        record.rail
        for record in gate_one.lastCatalog
        if record.status in {"pass", "fail"} and record.rail.key not in dependant_keys
    ]
    if not unrelated_executed:
        return _red(
            "r17-scenario-02",
            "no unrelated Gate-1 rail executed after the prerequisite failure",
        )
    return ScenarioOutcome(scenarioId="r17-scenario-02", state="green", findings=())


def _evaluate_03(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    if not evidence.faultRails:
        return _not_applicable("r17-scenario-03", "no fault rail was declared")
    treatment = evidence.treatment
    fault_keys = {rail.key for rail in evidence.faultRails}
    gate_one_failed = {rail.key for rail in treatment.gates[0].failedRails}
    if not fault_keys.intersection(gate_one_failed):
        return _not_applicable(
            "r17-scenario-03",
            "the declared fault rail did not fail in the Gate-1 catalog",
        )
    later_starts = [gate for gate in (2, 3, 4, 5) if treatment.gates[gate - 1].started]
    if later_starts:
        return _red(
            "r17-scenario-03",
            f"Gates {later_starts} started after the Gate-1 fault failure",
        )
    return ScenarioOutcome(scenarioId="r17-scenario-03", state="green", findings=())


def _evaluate_04(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    gate_one = evidence.treatment.gates[0]
    if gate_one.lastCatalogDisposition != "red" or not gate_one.lastCatalog:
        return _not_applicable("r17-scenario-04", "no Gate-1 red catalog was recorded")
    failed_keys = {rail.key for rail in gate_one.failedRails}
    pyright_failed = any(
        placement.rail.key in failed_keys for placement in _placements(evidence, "pyright")
    )
    if not pyright_failed:
        return _not_applicable(
            "r17-scenario-04", "the pyright rail did not fail in the Gate-1 catalog"
        )
    catalog_keys = {record.rail.key for record in gate_one.lastCatalog}
    missing = [rail.key for rail in evidence.companionRails if rail.key not in catalog_keys]
    if missing:
        return _red(
            "r17-scenario-04",
            "Gate-1 red catalog hid companion rails: " + ", ".join(sorted(missing)),
        )
    return ScenarioOutcome(scenarioId="r17-scenario-04", state="green", findings=())


def _evaluate_05(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    return _later_gates_zero_start(evidence, 2, "r17-scenario-05", (3, 4, 5))


def _evaluate_06(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    gate_two = evidence.treatment.gates[1]
    gate_three = evidence.treatment.gates[2]
    gate_two_green = gate_two.decision in {"pass-published", "pass-reused"} or (
        gate_two.lastCatalogDisposition == "green"
    )
    if not gate_two_green:
        return _not_applicable(
            "r17-scenario-06", "Gate 2 was not green, so Gate 3 never consumed its artifacts"
        )
    if gate_three.lastCatalogDisposition != "red" or not gate_three.lastCatalog:
        return _not_applicable(
            "r17-scenario-06", "no Gate-3 red catalog was recorded against offenders"
        )
    declared_offenders = {rail.key for rail in evidence.offenderRails}
    offender_failures = {
        rail.key for rail in gate_three.failedRails if rail.key in declared_offenders
    }
    declared = declared_offenders
    missing = sorted(declared - offender_failures)
    if missing:
        return _red(
            "r17-scenario-06",
            "Gate-3 catalog omitted CRAP/diff-coverage offenders: " + ", ".join(missing),
        )
    return ScenarioOutcome(scenarioId="r17-scenario-06", state="green", findings=())


def _evaluate_07(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    return _later_gates_zero_start(evidence, 3, "r17-scenario-07", (4, 5))


def _evaluate_08(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    return _later_gates_zero_start(evidence, 4, "r17-scenario-08", (5,))


def _evaluate_09(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    baseline = evidence.baseline
    if baseline is None:
        return _not_applicable(
            "r17-scenario-09", "no baseline leg was recorded for the memory repair pair"
        )
    if "memory-onboarding" not in evidence.changeClasses:
        return _not_applicable(
            "r17-scenario-09", "the replay did not declare a memory-only change class"
        )
    if baseline.gates[4].decision != "fail":
        return _not_applicable(
            "r17-scenario-09", "the baseline Gate 5 did not fail on memory inputs"
        )
    treatment = evidence.treatment
    prefix_reused = all(
        treatment.gates[gate - 1].decision == "pass-reused" for gate in (1, 2, 3, 4)
    )
    if not prefix_reused:
        return _red(
            "r17-scenario-09",
            "the memory repair did not reuse the exact green Gates 1-4 certificates",
        )
    if not treatment.gates[4].started:
        return _red("r17-scenario-09", "the memory repair did not re-run Gate 5 after the reuse")
    return ScenarioOutcome(scenarioId="r17-scenario-09", state="green", findings=())


def _evaluate_10(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    if "code" not in evidence.changeClasses:
        return _not_applicable("r17-scenario-10", "the replay did not declare a code input change")
    treatment = evidence.treatment
    invalidated = [gate for gate in (1, 2, 3, 4, 5) if treatment.gates[gate - 1].invalidated]
    if invalidated == [1, 2, 3, 4, 5]:
        return ScenarioOutcome(scenarioId="r17-scenario-10", state="green", findings=())
    if not invalidated:
        return _red("r17-scenario-10", "no gate certificate was invalidated by the code change")
    return _red(
        "r17-scenario-10",
        f"code change invalidated only gates {invalidated}, not the full Gates 1-5 closure",
    )


def _evaluate_11(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    declared = [change for change in evidence.changeClasses if change.startswith("gate-")]
    if not declared:
        return _not_applicable(
            "r17-scenario-11", "no per-gate profile/config change class was declared"
        )
    treatment = evidence.treatment
    invalidated = {gate for gate in (1, 2, 3, 4, 5) if treatment.gates[gate - 1].invalidated}
    for change in declared:
        start = int(change.removeprefix("gate-").removesuffix("-input"))
        expected = set(range(start, 6))
        if invalidated != expected:
            return _red(
                "r17-scenario-11",
                f"{change} invalidated gates {sorted(invalidated)}, "
                f"not its declared closure {sorted(expected)}",
            )
    return ScenarioOutcome(scenarioId="r17-scenario-11", state="green", findings=())


def _evaluate_12(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    metadata = [
        change
        for change in evidence.changeClasses
        if change in {"journal-review-approval-attempt", "metadata-only"}
    ]
    if not metadata:
        return _not_applicable(
            "r17-scenario-12", "no review/journal/approval metadata change was declared"
        )
    treatment = evidence.treatment
    if treatment.certificateInvalidationCount:
        return _red(
            "r17-scenario-12",
            f"metadata change invalidated {treatment.certificateInvalidationCount} certificates",
        )
    if not all(
        treatment.gates[gate - 1].decision in {"pass-published", "pass-reused"}
        for gate in (1, 2, 3, 4, 5)
    ):
        return _red(
            "r17-scenario-12", "metadata change did not preserve the exact green certificates"
        )
    return ScenarioOutcome(scenarioId="r17-scenario-12", state="green", findings=())


def _evaluate_13(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    treatment = evidence.treatment
    if not treatment.finalizationResumed:
        return _not_applicable("r17-scenario-13", "no finalization boundary resume was recorded")
    if treatment.certificateReuseCount < 5:
        return _red(
            "r17-scenario-13",
            "resumed finalization did not reuse the exact five green certificates",
        )
    started_after_resume = any(treatment.gates[gate - 1].started for gate in (1, 2, 3, 4, 5))
    if started_after_resume:
        return _red("r17-scenario-13", "an unchanged gate restarted after finalization resumed")
    return ScenarioOutcome(scenarioId="r17-scenario-13", state="green", findings=())


def _evaluate_14(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    if not evidence.baseline:
        return _not_applicable("r17-scenario-14", "no pre-commit baseline leg was provided")
    baseline_keys = {placement.rail.key for placement in evidence.baseline_placements()}
    treatment_keys = {placement.rail.key for placement in evidence.railPlacements}
    if not baseline_keys or not treatment_keys:
        return _not_applicable(
            "r17-scenario-14", "no pre-commit or closeout rail placements were provided"
        )
    if baseline_keys != treatment_keys:
        return _red(
            "r17-scenario-14",
            "pre-commit and closeout canonical rail definitions differ",
        )
    return ScenarioOutcome(scenarioId="r17-scenario-14", state="green", findings=())


def _evaluate_15(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    treatment = evidence.treatment
    if treatment.admissionRefused:
        return _red("r17-scenario-15", "admission refused before any certifying gate evidence")
    promoted = [
        gate
        for gate in (1, 2, 3, 4, 5)
        if treatment.gates[gate - 1].decision == "certificate-refused"
    ]
    if promoted:
        return _red(
            "r17-scenario-15",
            f"gates {promoted} refused certificates after a green catalog; "
            "the stream cannot certify",
        )
    if treatment.certificatePublishCount + treatment.certificateReuseCount == 0:
        return _not_applicable("r17-scenario-15", "no gate certificate was published or reused")
    return ScenarioOutcome(scenarioId="r17-scenario-15", state="green", findings=())


def _evaluate_16(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    problems = _scenario_16_problems(evidence)
    if problems is None:
        return _not_applicable(
            "r17-scenario-16",
            "two non-Agents-Remember repository fixtures are required for parity",
        )
    if problems:
        return _red("r17-scenario-16", "; ".join(problems))
    return ScenarioOutcome(scenarioId="r17-scenario-16", state="green", findings=())


def _scenario_16_problems(
    evidence: ReplayScenarioEvidence,
) -> list[str] | None:
    """Red reasons, or None when the parity evidence is absent entirely."""
    profiles = evidence.profiles
    if len(profiles) < 2:
        return None
    left, right = profiles[0], profiles[1]
    problems: list[str] = []
    if left.repositoryId == "agents-remember" or right.repositoryId == "agents-remember":
        problems.append(
            "repository-generic parity must not depend on the Agents Remember reference"
        )
    if left.repositoryId == right.repositoryId:
        problems.append("the two repository fixtures must be distinct repositories")
    if left.toolIdentity == right.toolIdentity:
        problems.append("the two repository fixtures must use different language/test/E2E tools")
    if left.frameworkContract != right.frameworkContract:
        problems.append(
            "the two repository fixtures must share the framework order/result contract"
        )
    for profile in profiles:
        covered = {placement.gate for placement in profile.placements}
        if set(range(1, 5)) - covered:
            problems.append(f"{profile.repositoryId} does not own a Gate 1-4 profile population")
    return problems


def _evaluate_17(evidence: ReplayScenarioEvidence) -> ScenarioOutcome:
    reference = _reference_profile(evidence)
    if reference is None:
        return _not_applicable(
            "r17-scenario-17", "the Agents Remember migrated profile was not provided"
        )
    missing = _scenario_17_missing(reference)
    if missing:
        return _red("r17-scenario-17", "migrated profile lacks: " + ", ".join(missing))
    return ScenarioOutcome(scenarioId="r17-scenario-17", state="green", findings=())


def _scenario_17_missing(profile: ReplayProfileSnapshot) -> list[str]:
    """Gate placements the migrated reference profile must still declare."""
    missing: list[str] = []
    expectations = {
        2: ("ordinary-test-suite", "pytest in Gate 2"),
        3: ("post-test-quality", "CRAP/diff coverage in Gate 3"),
        4: ("integration-test", "real-Codex E2E in Gate 4"),
        5: ("memory-quality", "memory in Gate 5"),
    }
    for gate, (rail_class, label) in sorted(expectations.items()):
        if not _class_rail(profile, gate, rail_class):
            missing.append(label)
    return missing


GateLiteral = Literal[1, 2, 3, 4, 5]


def _later_gates_zero_start(
    evidence: ReplayScenarioEvidence,
    failing_gate: GateLiteral,
    scenario_id: str,
    later_gates: tuple[GateLiteral, ...],
) -> ScenarioOutcome:
    treatment = evidence.treatment
    failing = treatment.gates[failing_gate - 1]
    if failing.lastCatalogDisposition != "red":
        return _not_applicable(scenario_id, f"Gate {failing_gate} did not produce a red catalog")
    started = [gate for gate in later_gates if treatment.gates[gate - 1].started]
    if started:
        return _red(
            scenario_id,
            f"Gates {started} started after the Gate-{failing_gate} red catalog",
        )
    return ScenarioOutcome(scenarioId=scenario_id, state="green", findings=())


def _placements(
    evidence: ReplayScenarioEvidence,
    prefix: str,
) -> tuple[ReplayRailPlacement, ...]:
    return tuple(
        placement
        for placement in evidence.railPlacements
        if placement.rail.railId.startswith(prefix)
    )


def _reference_profile(
    evidence: ReplayScenarioEvidence,
) -> ReplayProfileSnapshot | None:
    """The Agents Remember migrated reference profile, when the evidence carries it."""
    for profile in evidence.profiles:
        if profile.repositoryId == "agents-remember":
            return profile
    return None


def _class_rail(
    profile: ReplayProfileSnapshot,
    gate: int,
    rail_class: str,
) -> bool:
    """Whether the profile places at least one enforcing rail of the class at the gate."""
    return any(
        placement.gate == gate
        and placement.railClass == rail_class
        and placement.posture == "enforcing"
        for placement in profile.placements
    )


_EVALUATORS: dict[str, Callable[[ReplayScenarioEvidence], ScenarioOutcome]] = {
    "r17-scenario-01": _evaluate_01,
    "r17-scenario-02": _evaluate_02,
    "r17-scenario-03": _evaluate_03,
    "r17-scenario-04": _evaluate_04,
    "r17-scenario-05": _evaluate_05,
    "r17-scenario-06": _evaluate_06,
    "r17-scenario-07": _evaluate_07,
    "r17-scenario-08": _evaluate_08,
    "r17-scenario-09": _evaluate_09,
    "r17-scenario-10": _evaluate_10,
    "r17-scenario-11": _evaluate_11,
    "r17-scenario-12": _evaluate_12,
    "r17-scenario-13": _evaluate_13,
    "r17-scenario-14": _evaluate_14,
    "r17-scenario-15": _evaluate_15,
    "r17-scenario-16": _evaluate_16,
    "r17-scenario-17": _evaluate_17,
}


__all__ = [
    "REPLAY_ACCEPTANCE_SCENARIOS",
    "CertificationContractFinding",
    "GateRunMeasurement",
    "RailIdentity",
    "ReplayRailPlacement",
    "ReplayScenarioEvidence",
    "ReplayScenarioExpectation",
    "RunMeasurement",
    "ScenarioOutcome",
    "ScenarioState",
    "evaluate_all_replay_scenarios",
    "evaluate_replay_scenario",
]
