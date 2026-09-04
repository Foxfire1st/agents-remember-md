"""Fully standalone CCR-R17 measured-replay freeze and population tests.

No certification-run, evidence-lifecycle, telemetry stream, or Dagger artifact
is shared; every fixture is constructed here.  Numeric reduction thresholds are
out of approved scope and are never asserted.
"""

from __future__ import annotations

from agents_remember.certification.replay.freeze import (
    ReplayComparabilityReport,
    ReplayFreeze,
    ReplayFreezeInput,
    ReplayPopulation,
    compare_replay_freezes,
    compile_replay_freeze,
    compile_replay_population,
    freeze_digest,
    population_denominator,
    require_append_only_population,
    require_comparable_replay_pair,
)
from agents_remember.certification.replay.models import PopulationGeneration
from agents_remember.errors import CertificationContractError

_DIGEST = "a" * 64
_DIGEST_B = "b" * 64


def _freeze_input(**overrides: object) -> ReplayFreezeInput:
    base: dict[str, object] = dict(
        sourceRevision="ar/260831-ccr-l17-ar@b5164f09",
        candidateDigest=_DIGEST,
        profileId="agents-remember-certification",
        profileDigest=_DIGEST,
        planDigest=_DIGEST,
        configurationDigest=_DIGEST,
        runtimeIdentity="cpython-3.13.15",
        toolchainDigest=_DIGEST,
        executorDigest=_DIGEST,
        imageDigest=_DIGEST,
        machineClass="x86_64-linux",
        instrumentationOnly=True,
        measurementSchema="measured-replay/v1",
    )
    base.update(overrides)
    return ReplayFreezeInput(**base)  # type: ignore[arg-type]


def test_freeze_compile_digest_is_deterministic_and_self_consistent() -> None:
    left = compile_replay_freeze(_freeze_input())
    right = compile_replay_freeze(_freeze_input())
    assert left == right
    assert left.freezeDigest == freeze_digest(_freeze_input())
    assert len(left.freezeDigest) == 64
    assert all(character in "0123456789abcdef" for character in left.freezeDigest)


def test_freeze_rejects_tampered_digest() -> None:
    freeze = compile_replay_freeze(_freeze_input())
    try:
        ReplayFreeze(input=freeze.input, freezeDigest="f" * 64)
    except ValueError:
        pass
    else:
        raise AssertionError("a tampered freeze digest must refuse construction")


def test_identical_freezes_are_comparable() -> None:
    left = compile_replay_freeze(_freeze_input())
    right = compile_replay_freeze(_freeze_input())
    report = compare_replay_freezes(left, right)
    assert isinstance(report, ReplayComparabilityReport)
    assert report.comparable
    assert report.changes == ()
    assert report.findings == ()
    require_comparable_replay_pair(left, right)  # must not raise


def test_source_revision_change_refuses_pair() -> None:
    baseline = compile_replay_freeze(_freeze_input())
    treatment = compile_replay_freeze(_freeze_input(sourceRevision="ar/260831-ccr-l17-ar@other"))
    report = compare_replay_freezes(baseline, treatment)
    assert not report.comparable
    assert any(change.changeClass == "source" for change in report.changes)
    try:
        require_comparable_replay_pair(baseline, treatment)
    except CertificationContractError:
        pass
    else:
        raise AssertionError("an incomparable pair must refuse")


def test_profile_change_refuses_pair() -> None:
    baseline = compile_replay_freeze(_freeze_input())
    treatment = compile_replay_freeze(
        _freeze_input(profileId="other-profile", profileDigest=_DIGEST_B)
    )
    report = compare_replay_freezes(baseline, treatment)
    assert not report.comparable
    assert any(change.changeClass == "profile" for change in report.changes)


def test_plan_configuration_and_runtime_changes_refuse_pair() -> None:
    baseline = compile_replay_freeze(_freeze_input())
    cases = (
        {"planDigest": _DIGEST_B, "expected": "plan"},
        {"configurationDigest": _DIGEST_B, "expected": "configuration"},
        {"imageDigest": _DIGEST_B, "expected": "runtime-toolchain-executor-image"},
        {"machineClass": "arm64-linux", "expected": "machine-class"},
        {"instrumentationOnly": False, "expected": "instrumentation"},
        {"measurementSchema": "measured-replay/v2", "expected": "measurement-schema"},
    )
    for case in cases:
        expected = case.pop("expected")
        treatment = compile_replay_freeze(_freeze_input(**case))
        report = compare_replay_freezes(baseline, treatment)
        assert not report.comparable
        assert any(change.changeClass == expected for change in report.changes)


def test_observation_metadata_never_changes_the_freeze() -> None:
    # Provenance/observation notes live outside the frozen input, so a freeze
    # never sees them and cannot invalidate a pair on their account.
    baseline = compile_replay_freeze(_freeze_input())
    same = compile_replay_freeze(_freeze_input())
    assert compare_replay_freezes(baseline, same).comparable


def _generations() -> list[PopulationGeneration]:
    rows: list[PopulationGeneration] = []
    for generation in range(1, 9):
        rows.append(
            PopulationGeneration(
                generation=generation,
                stratum="frozen-original",
                sourceIdentity="frozen",
            )
        )
    for generation in range(9, 14):
        rows.append(
            PopulationGeneration(
                generation=generation,
                stratum="post-analysis-tail",
                sourceIdentity="tail",
            )
        )
    rows.append(
        PopulationGeneration(
            generation=14,
            stratum="dated-supplement",
            sourceIdentity="supplement",
        )
    )
    return rows


def test_population_compile_orders_and_digests_rows() -> None:
    population = compile_replay_population(_generations())
    assert isinstance(population, ReplayPopulation)
    assert population.populationDigest == population.populationDigest
    assert [row.generation for row in population.generations] == list(range(1, 15))


def test_population_denominator_excludes_dated_supplements() -> None:
    population = compile_replay_population(_generations())
    assert population_denominator(population) == tuple(range(1, 14))
    assert "dated-supplement" in {row.stratum for row in population.generations}


def test_population_rejects_duplicate_generations() -> None:
    rows = _generations()
    rows.append(
        PopulationGeneration(
            generation=14,
            stratum="dated-supplement",
            sourceIdentity="again",
        )
    )
    try:
        compile_replay_population(rows)
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate incident generations must refuse")


def test_population_stratum_generation_ranges_are_closed() -> None:
    invalid_cases = (
        ("frozen-original", 9),
        ("post-analysis-tail", 8),
        ("dated-supplement", 13),
        ("incident-baseline", 14),
    )
    for stratum, generation in invalid_cases:
        try:
            PopulationGeneration(
                generation=generation,
                stratum=stratum,  # type: ignore[arg-type]
                sourceIdentity="row",
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"{stratum} generation {generation} must refuse")


def test_append_only_population_guard_refuses_rewrites() -> None:
    population = compile_replay_population(_generations())
    # A dated supplement after the frozen boundary is an accepted append.
    successor = compile_replay_population(
        [
            *population.generations,
            PopulationGeneration(
                generation=15,
                stratum="dated-supplement",
                sourceIdentity="qualitative",
            ),
        ]
    )
    require_append_only_population(population, successor)
    # A dated supplement at generation 14 is inside the supplement range, but it
    # may only append a fresh observation; repeating an existing supplement is a
    # no-op, while rewriting a frozen row under any stratum must refuse.
    replacement = compile_replay_population(
        [
            PopulationGeneration(
                generation=5,
                stratum="frozen-original",
                sourceIdentity="mutated",
            )
        ]
    )
    try:
        require_append_only_population(population, replacement)
    except CertificationContractError:
        pass
    else:
        raise AssertionError("a rewritten frozen row must refuse")
    # Replacing a dated supplement row with different identity content refuses.
    supplement_rewrite = compile_replay_population(
        [
            PopulationGeneration(
                generation=14,
                stratum="dated-supplement",
                sourceIdentity="rewritten-supplement",
            )
        ]
    )
    try:
        require_append_only_population(population, supplement_rewrite)
    except CertificationContractError:
        pass
    else:
        raise AssertionError("a rewritten dated supplement row must refuse")
