"""Closed-model and terminal-publication edges for certification contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

import pytest
from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    compile_gate_result_manifest,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.lifecycle_models import (
    CorrectiveInputChange,
    DurableFinalizationLeg,
    ExactCandidateObservation,
    FinalizationJournalState,
    FinalizationLeg,
    FinalizationLegState,
    RedCatalogDisposition,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    RailApplicability,
    RailIdentity,
    RailResult,
    RailTerminalObservation,
    RegistryProfile,
    canonical_execution_waves,
)
from agents_remember.errors import (
    CertificationContractError,
    HarnessAdapterBusyError,
)
from certification_registry_test_support import (
    _CANDIDATE,
    _DIGEST,
    ObservationSpec,
    RailSpec,
    _canonical_registry,
    _finding_codes,
    _gate,
    _identity,
    _manifest,
    _plan,
    _rebuild_gate_plan,
    _registry,
    _result,
)
from pydantic import BaseModel, ValidationError


def _digest_payload(
    value: BaseModel,
    digest_field: str,
    **updates: object,
) -> dict[str, object]:
    payload = value.model_dump(mode="json", exclude={digest_field})
    payload.update(updates)
    payload[digest_field] = content_digest(payload)
    return payload


def _green_gate_one_manifest() -> tuple[CertificationPlan, GatePlan, GateResultManifest]:
    plan = _plan()
    gate = _gate(plan, 1)
    results = tuple(_result(gate, ObservationSpec(rail.identity.railId)) for rail in gate.rails)
    return plan, gate, _manifest(plan, gate, results, altitude="certifying")


def _finalization_leg(leg: FinalizationLeg, state: FinalizationLegState) -> DurableFinalizationLeg:
    if state == "not-applicable":
        return DurableFinalizationLeg(leg=leg, state=state)
    values = {"authorityDigest": _DIGEST}
    if state in {"intent", "proven"}:
        values["intendedOutputDigest"] = "b" * 64
    if state == "proven":
        values["provenOutputDigest"] = "b" * 64
    return DurableFinalizationLeg(leg=leg, state=state, **values)


@pytest.mark.parametrize(
    ("status", "population", "reason"),
    [
        ("applicable", None, None),
        ("applicable", "population", "not needed"),
        ("not-applicable", "population", "not selected"),
        ("not-applicable", None, None),
    ],
)
def test_applicability_status_requires_its_exact_payload(
    status: Literal["applicable", "not-applicable"],
    population: str | None,
    reason: str | None,
) -> None:
    with pytest.raises(ValidationError):
        RailApplicability(
            profileId="portable-ci",
            status=status,
            selectionIdentity="selection:portable",
            population=population,
            reason=reason,
        )


def test_registry_and_plan_digests_reject_content_drift() -> None:
    registry = _registry()
    with pytest.raises(ValidationError, match="registry digest"):
        CanonicalRailRegistry(registry=registry, registryDigest=_DIGEST)

    plan = _plan()
    gate = _gate(plan, 1)
    with pytest.raises(ValidationError, match="gate plan digest"):
        GatePlan.model_validate(
            {
                **gate.model_dump(mode="json", exclude={"planDigest"}),
                "planDigest": _DIGEST,
            }
        )
    with pytest.raises(ValidationError, match="certification plan digest"):
        CertificationPlan.model_validate(
            {
                **plan.model_dump(mode="json", exclude={"planDigest"}),
                "planDigest": _DIGEST,
            }
        )


def test_execution_waves_cover_fanout_and_reject_a_cycle() -> None:
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    registry = _registry(
        (
            RailSpec("root", 1),
            RailSpec("left", 1, prerequisites=("root",)),
            RailSpec("right", 1, prerequisites=("root",)),
            RailSpec("join", 1, prerequisites=("root", "left")),
        ),
        profile=profile,
    )
    rails = _gate(_plan(registry), 1).rails
    assert canonical_execution_waves(rails) == (
        (_identity("root"),),
        (_identity("left"), _identity("right")),
        (_identity("join"),),
    )

    by_id = {rail.identity.railId: rail for rail in rails}
    left = by_id["left"]
    right = by_id["right"]
    cycle = (
        left.model_copy(update={"prerequisites": (right.identity,)}),
        right.model_copy(update={"prerequisites": (left.identity,)}),
    )
    with pytest.raises(ValueError, match="dependency cycle"):
        canonical_execution_waves(cycle)


@pytest.mark.parametrize("defect", ["duplicate", "wrong-gate", "wrong-profile"])
def test_gate_plan_catalog_rejects_each_identity_dimension(defect: str) -> None:
    gate = _gate(_plan(), 1)
    rails = list(gate.rails)
    if defect == "duplicate":
        rails.append(rails[0])
    elif defect == "wrong-gate":
        rails[0] = rails[0].model_copy(update={"gate": 2})
    else:
        applicability = rails[0].applicability.model_copy(update={"profileId": "foreign"})
        rails[0] = rails[0].model_copy(update={"applicability": applicability})

    with pytest.raises(ValidationError):
        GatePlan.model_validate(
            _digest_payload(
                gate,
                "planDigest",
                rails=[item.model_dump(mode="json") for item in rails],
            )
        )


@pytest.mark.parametrize("defect", ["unordered", "missing-prefix", "incomplete-certifying"])
def test_certification_plan_catalog_rejects_each_gate_dimension(defect: str) -> None:
    plan = _plan()
    gates = list(plan.gates)
    profile_kind: Literal["certifying", "diagnostic"] = "certifying"
    if defect == "unordered":
        gates = [gates[1], gates[0], *gates[2:]]
    elif defect == "missing-prefix":
        gates = [gates[0], gates[2]]
        profile_kind = "diagnostic"
    else:
        gates = gates[:4]

    with pytest.raises(ValidationError):
        CertificationPlan.model_validate(
            _digest_payload(
                plan,
                "planDigest",
                gates=[item.model_dump(mode="json") for item in gates],
                profileKind=profile_kind,
            )
        )


def test_certification_plan_rejects_a_gate_bound_to_another_candidate() -> None:
    plan = _plan()
    foreign = CandidateIdentity(kind="content-digest", value="f" * 64)
    forged_gate = _rebuild_gate_plan(
        plan.gates[0],
        candidateIdentity=foreign.model_dump(mode="json"),
    )
    gates = [forged_gate, *plan.gates[1:]]

    with pytest.raises(ValidationError, match="gate plan identity"):
        CertificationPlan.model_validate(
            _digest_payload(
                plan,
                "planDigest",
                gates=[item.model_dump(mode="json") for item in gates],
            )
        )


@pytest.mark.parametrize(
    "updates",
    (
        {"worktreeStatus": "conflicted", "conflictedPaths": []},
        {"worktreeStatus": "conflicted", "conflictedPaths": ["b.py", "a.py"]},
    ),
)
def test_exact_candidate_rejects_contradictory_conflict_shapes(
    updates: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "taskId": "leaf",
        "contractPath": "/coordination/contract.md",
        "lifecycleAuthorityDigest": _DIGEST,
        "repositoryId": "repository",
        "sourceBranchRef": "refs/heads/leaf",
        "sourceBranchTip": "a" * 40,
        "candidateCodeTree": CandidateIdentity(kind="git-tree", value="c" * 40).model_dump(
            mode="json"
        ),
        "topologyIdentityDigest": _DIGEST,
        "taskIntentIdentityDigest": _DIGEST,
        "normalizedCommitIntentDigest": _DIGEST,
        "mutationAuthorityDigest": _DIGEST,
        "sourceAuthorityDigest": _DIGEST,
        "worktreeRuleDigest": _DIGEST,
        "generatedInputsDigest": _DIGEST,
        "mutationAuthorityStatus": "valid",
        "sourceAuthorityStatus": "valid",
        "branchAuthorityStatus": "valid",
        "worktreeStatus": "admissible",
        "generatedArtifactStatus": "current",
    }
    with pytest.raises(ValidationError):
        ExactCandidateObservation.model_validate({**payload, **updates})


@pytest.mark.parametrize(
    ("state", "authority", "intended", "proven"),
    (
        ("not-applicable", _DIGEST, None, None),
        ("pending", None, None, None),
        ("pending", _DIGEST, _DIGEST, None),
        ("intent", _DIGEST, None, None),
        ("intent", _DIGEST, _DIGEST, _DIGEST),
        ("proven", _DIGEST, None, _DIGEST),
        ("proven", _DIGEST, _DIGEST, None),
    ),
)
def test_finalization_leg_rejects_state_shape_mismatches(
    state: FinalizationLegState,
    authority: str | None,
    intended: str | None,
    proven: str | None,
) -> None:
    with pytest.raises(ValidationError):
        DurableFinalizationLeg(
            leg="code-commit",
            state=state,
            authorityDigest=authority,
            intendedOutputDigest=intended,
            provenOutputDigest=proven,
        )


def test_red_disposition_and_journal_reject_noncanonical_shapes() -> None:
    rail = RailIdentity(railId="rail", version="1.0.0")
    change = CorrectiveInputChange(
        inputKind="candidate-code-tree",
        inputId="candidate",
        beforeDigest="a" * 40,
        afterDigest="b" * 40,
    )
    common = {
        "rail": rail,
        "priorStatus": "fail",
        "priorResultDigest": _DIGEST,
        "correctiveOwner": "owner",
        "rationale": "exact corrective evidence",
    }
    invalid = (
        {"disposition": "direct-repair", "changedInputs": ()},
        {
            "disposition": "repaired-root",
            "repairedRoot": rail,
            "changedInputs": (change,),
        },
        {"disposition": "direct-repair", "changedInputs": (change, change)},
    )
    for updates in invalid:
        with pytest.raises(ValidationError):
            RedCatalogDisposition(**common, **updates)

    order: tuple[FinalizationLeg, ...] = (
        "code-commit",
        "external-memory-commit",
        "ledger-commit",
        "contract-finalization",
    )

    def journal(states: tuple[FinalizationLegState, ...]) -> FinalizationJournalState:
        return FinalizationJournalState(
            journalAuthorityDigest=_DIGEST,
            legs=tuple(
                _finalization_leg(leg, state) for leg, state in zip(order, states, strict=True)
            ),
        )

    with pytest.raises(ValidationError, match="durable leg order"):
        FinalizationJournalState(
            journalAuthorityDigest=_DIGEST,
            legs=tuple(_finalization_leg(leg, "not-applicable") for leg in reversed(order)),
        )
    with pytest.raises(ValidationError, match="at most one"):
        journal(("intent", "intent", "not-applicable", "not-applicable"))
    with pytest.raises(ValidationError, match="not monotonic"):
        journal(("intent", "proven", "not-applicable", "not-applicable"))
    pending = journal(("proven", "pending", "pending", "pending"))
    assert pending.next_leg == "external-memory-commit"
    intent = journal(("intent", "pending", "not-applicable", "not-applicable"))
    assert intent.next_leg == "code-commit"
    assert journal(("proven", "proven", "proven", "proven")).next_leg is None


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "blocked", "blockedBy": []},
        {"status": "pass", "blockedBy": [_identity("types").model_dump(mode="json")]},
    ],
)
def test_rail_result_status_rejects_illegal_blocker_payloads(updates: dict[str, object]) -> None:
    gate = _gate(_plan(), 1)
    result = _result(gate, ObservationSpec("lint"))

    with pytest.raises(ValidationError):
        RailResult.model_validate(_digest_payload(result, "resultDigest", **updates))


def test_not_applicable_result_cannot_publish_artifacts() -> None:
    gate = _gate(_plan(), 2)
    result = _result(gate, ObservationSpec("suite"))

    with pytest.raises(ValidationError, match="cannot publish artifacts"):
        RailResult.model_validate(_digest_payload(result, "resultDigest", status="not-applicable"))


@pytest.mark.parametrize("member", ["blockedBy", "artifacts", "evidence"])
def test_rail_result_rejects_duplicate_member_identities(member: str) -> None:
    gate = _gate(_plan(), 2 if member == "artifacts" else 1)
    rail_id = "suite" if member == "artifacts" else "lint"
    result = _result(gate, ObservationSpec(rail_id))
    updates: dict[str, object] = {}
    if member == "blockedBy":
        blocker = _identity("types").model_dump(mode="json")
        updates = {"status": "blocked", "blockedBy": [blocker, blocker]}
    else:
        values = result.model_dump(mode="json")[member]
        updates = {member: [*values, *values]}

    with pytest.raises(ValidationError, match="must be unique"):
        RailResult.model_validate(_digest_payload(result, "resultDigest", **updates))


def test_rail_result_digest_rejects_content_drift() -> None:
    gate = _gate(_plan(), 1)
    result = _result(gate, ObservationSpec("lint"))
    with pytest.raises(ValidationError, match="result digest"):
        RailResult.model_validate(
            {
                **result.model_dump(mode="json", exclude={"resultDigest"}),
                "resultDigest": _DIGEST,
            }
        )


@pytest.mark.parametrize(
    "defect",
    ["disposition", "altitude", "duplicate", "mixed-gate", "mixed-plan", "digest"],
)
def test_gate_manifest_rejects_each_terminal_identity_dimension(defect: str) -> None:
    _plan_value, _gate_value, manifest = _green_gate_one_manifest()
    updates: dict[str, object] = {}
    if defect == "disposition":
        updates = {"disposition": "red"}
    elif defect == "altitude":
        updates = {"altitude": "diagnostic"}
    elif defect == "duplicate":
        results = manifest.model_dump(mode="json")["railResults"]
        updates = {"railResults": [*results, results[0]]}
    elif defect in {"mixed-gate", "mixed-plan"}:
        results = list(manifest.railResults)
        result_updates = {"gate": 2} if defect == "mixed-gate" else {"gatePlanDigest": "b" * 64}
        results[0] = RailResult.model_validate(
            _digest_payload(results[0], "resultDigest", **result_updates)
        )
        updates = {"railResults": [item.model_dump(mode="json") for item in results]}
    elif defect == "digest":
        payload = manifest.model_dump(mode="json", exclude={"manifestDigest"})
        with pytest.raises(ValidationError, match="manifest digest"):
            GateResultManifest.model_validate({**payload, "manifestDigest": _DIGEST})
        return

    with pytest.raises(ValidationError):
        GateResultManifest.model_validate(_digest_payload(manifest, "manifestDigest", **updates))


def test_plan_compilation_reports_invalid_registry_and_unknown_profile() -> None:
    registry = _registry()
    invalid_rail = registry.rails[0].model_copy(update={"gate": 2})
    invalid = canonicalize_registry(
        registry.model_copy(update={"rails": (invalid_rail, *registry.rails[1:])})
    )
    with pytest.raises(CertificationContractError) as invalid_error:
        compile_certification_plan(
            invalid,
            profile_id="portable-ci",
            candidate_identity=_CANDIDATE,
        )
    assert "wrong-gate-classification" in _finding_codes(invalid_error.value)

    with pytest.raises(CertificationContractError) as unknown_error:
        compile_certification_plan(
            _canonical_registry(),
            profile_id="unknown",
            candidate_identity=_CANDIDATE,
        )
    assert _finding_codes(unknown_error.value) == {"unknown-profile"}


def test_result_construction_refuses_an_unplanned_observation() -> None:
    gate = _gate(_plan(), 1)
    observation = RailTerminalObservation(
        rail=_identity("unplanned"),
        status="fail",
        code="unplanned-fail",
    )
    with pytest.raises(CertificationContractError) as caught:
        build_rail_result(gate, observation)
    assert _finding_codes(caught.value) == {"unplanned-rail-result"}


def test_manifest_refuses_a_valid_gate_plan_not_admitted_by_its_parent() -> None:
    canonical = _canonical_registry()
    plan = _plan()
    foreign = CandidateIdentity(kind="content-digest", value="f" * 64)
    gate = _rebuild_gate_plan(
        _gate(plan, 1),
        candidateIdentity=foreign.model_dump(mode="json"),
    )
    results = tuple(_result(gate, ObservationSpec(rail.identity.railId)) for rail in gate.rails)
    admission = GateResultAdmission(
        profileId="portable-ci",
        candidateIdentity=_CANDIDATE,
        altitude="certifying",
    )

    with pytest.raises(CertificationContractError) as caught:
        compile_gate_result_manifest(canonical, plan, gate, results, admission)
    assert _finding_codes(caught.value) == {"gate-plan-not-admitted"}


@pytest.mark.parametrize("defect", ["unplanned", "duplicate", "identity", "applicability"])
def test_manifest_catalog_reports_each_independent_result_defect(defect: str) -> None:
    canonical = _canonical_registry()
    plan = _plan()
    gate = _gate(plan, 1)
    results = [_result(gate, ObservationSpec(rail.identity.railId)) for rail in gate.rails]
    expected = ""
    if defect == "unplanned":
        other = _registry(
            (
                RailSpec("lint", 1),
                RailSpec("types", 1),
                RailSpec("package", 1, prerequisites=("lint",)),
                RailSpec("extra", 1),
            ),
            profile=RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,)),
        )
        other_gate = _gate(_plan(other), 1)
        results.append(_result(other_gate, ObservationSpec("extra")))
        expected = "unplanned-rail-result"
    elif defect == "duplicate":
        results.append(results[0])
        expected = "duplicate-rail-result"
    elif defect == "identity":
        results[0] = RailResult.model_validate(
            _digest_payload(results[0], "resultDigest", correctiveOwner="foreign-owner")
        )
        expected = "rail-result-plan-mismatch"
    else:
        results[0] = RailResult.model_validate(
            _digest_payload(results[0], "resultDigest", status="not-applicable")
        )
        expected = "rail-result-applicability-mismatch"

    admission = GateResultAdmission(
        profileId="portable-ci",
        candidateIdentity=_CANDIDATE,
        altitude="certifying",
    )
    with pytest.raises(CertificationContractError) as caught:
        compile_gate_result_manifest(canonical, plan, gate, results, admission)
    assert expected in _finding_codes(caught.value)


def test_contract_error_freezer_rejects_unsafe_values_and_copies_bytearrays() -> None:
    non_string_key = cast(str, 1)
    bad_payload: Mapping[str, object] = {non_string_key: "not-a-string-key"}
    with pytest.raises(TypeError, match="mapping keys"):
        CertificationContractError("failed", [{"payload": bad_payload}])
    error = CertificationContractError("failed", [{"payload": bytearray(b"safe-copy")}])
    assert error.findings[0]["payload"] == b"safe-copy"
    with pytest.raises(TypeError, match="immutable JSON-like"):
        CertificationContractError("failed", [{"payload": {"mutable-set"}}])


def test_busy_adapter_error_refuses_uncertain_send_state() -> None:
    with pytest.raises(ValueError, match="must certify"):
        HarnessAdapterBusyError("busy", may_have_sent=True)
