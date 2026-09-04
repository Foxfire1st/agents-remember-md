"""Standalone CCR-R14 planning and predecessor-barrier tests.

Covers plan-record compilation against the canonical R11 registry and
certifying plan, plus the exact Gate-1..3 must-not-run barriers (missing,
red, non-certifying, candidate-mismatched, or differently bound predecessors
refuse before any scenario step).  Fully standalone: it imports only the
leaf-local builder module and the package under test.
"""

from __future__ import annotations

import pytest
from agents_remember.certification.final_codex.planning import (
    compile_final_codex_plan_record,
    final_codex_gate_plan,
    require_gates_one_to_three_green,
)
from agents_remember.certification.models import CertificationPlan
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.errors import CertificationContractError
from test_final_codex_models import (
    CANDIDATE,
    OTHER_CANDIDATE,
    SCENARIO_VERSION,
    certifying_plan,
    green_gates,
    manifest_for,
    scenario_registry,
)


def store_codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


def other_plan(registry) -> CertificationPlan:
    return compile_certification_plan(
        registry,
        profile_id="portable-ci",
        candidate_identity=OTHER_CANDIDATE,
    )


class FinalCodexPlanningTests:
    def test_plan_record_binds_exact_certifying_plan(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        record = compile_final_codex_plan_record(
            registry,
            certifying_plan=plan,
            candidate_identity=CANDIDATE,
            scenario_version=SCENARIO_VERSION,
            plan_version="1.0.0",
        )
        assert record.registryDigest == registry.registryDigest
        assert record.certifyingPlanDigest == plan.planDigest
        assert record.gate == 4
        gate_plan = final_codex_gate_plan(record, plan)
        assert gate_plan.gate == 4
        assert gate_plan.planDigest == record.gatePlanDigest

    def test_plan_record_refuses_a_foreign_certifying_plan(self) -> None:
        registry = scenario_registry()
        foreign = other_plan(registry)
        with pytest.raises(CertificationContractError) as error:
            compile_final_codex_plan_record(
                registry,
                certifying_plan=foreign,
                candidate_identity=CANDIDATE,
                scenario_version=SCENARIO_VERSION,
                plan_version="1.0.0",
            )
        assert "final-codex-candidate-mismatch" in store_codes(error.value)

    def test_green_gates_admit_and_non_green_refuse(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        require_gates_one_to_three_green(
            CANDIDATE,
            green_gates(registry),
            certifying_plan=plan,
        )
        red_three = manifest_for(registry, plan, 3, red=True)
        with pytest.raises(CertificationContractError) as error:
            require_gates_one_to_three_green(
                CANDIDATE,
                (red_three,),
                certifying_plan=plan,
            )
        assert "final-codex-prerequisite-incomplete" in store_codes(error.value)
        green = green_gates(registry)
        with pytest.raises(CertificationContractError) as error:
            require_gates_one_to_three_green(
                CANDIDATE,
                (green[0], green[1], red_three),
                certifying_plan=plan,
            )
        assert "final-codex-prerequisite-not-green" in store_codes(error.value)

    def test_candidate_mismatch_refuses(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        foreign = other_plan(registry)
        foreign_green = tuple(manifest_for(registry, foreign, gate) for gate in (1, 2, 3))
        with pytest.raises(CertificationContractError) as error:
            require_gates_one_to_three_green(
                CANDIDATE,
                foreign_green,
                certifying_plan=plan,
            )
        assert "final-codex-prerequisite-candidate-mismatch" in store_codes(error.value)

    def test_altitude_mismatch_refuses(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        green = green_gates(registry)
        # model_copy skips revalidation so the diagnostic-altitude defect that
        # the executor can receive from a foreign lane is observable.
        diag_three = green[2].model_copy(update={"altitude": "diagnostic"})
        with pytest.raises(CertificationContractError) as error:
            require_gates_one_to_three_green(
                CANDIDATE,
                (green[0], green[1], diag_three),
                certifying_plan=plan,
            )
        assert "final-codex-prerequisite-not-certifying" in store_codes(error.value)

    def test_gate_plan_mismatch_refuses(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        green = green_gates(registry)
        # a stale manifest bound to a different gate plan digest is refused
        stale_three = green[2].model_copy(update={"gatePlanDigest": "f" * 64})
        with pytest.raises(CertificationContractError) as error:
            require_gates_one_to_three_green(
                CANDIDATE,
                (green[0], green[1], stale_three),
                certifying_plan=plan,
            )
        assert "final-codex-prerequisite-gate-plan-mismatch" in store_codes(error.value)
