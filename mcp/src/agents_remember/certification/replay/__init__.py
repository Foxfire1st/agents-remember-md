"""CCR-R17-v3 measured replay, freeze, and comparison-report surface.

The package owns the correctness replay protocol contracts that are approved for
260831-CCR: exact replay freeze identities, the append-only three-view incident
baseline population, a deterministic span analyzer over the R16 telemetry span
vocabulary, a measured-run reducer over an R16 event export, the seventeen
mandatory acceptance scenarios projected to machine-readable outcomes, and the
baseline-vs-treatment comparison report.  Numeric reduction thresholds are
intentionally out of scope and never appear in these records.
"""

from agents_remember.certification.replay.compare import (
    ReplayComparisonReport,
    build_replay_comparison_report,
)
from agents_remember.certification.replay.freeze import (
    PopulationGeneration,
    ReplayComparabilityReport,
    ReplayFreeze,
    ReplayFreezeInput,
    ReplayPopulation,
    ReplayStratum,
    compare_replay_freezes,
    compile_replay_freeze,
    compile_replay_population,
    freeze_digest,
    population_denominator,
    require_comparable_replay_pair,
)
from agents_remember.certification.replay.measure import (
    GateRunMeasurement,
    RunMeasurement,
    measure_replay_run,
)
from agents_remember.certification.replay.models import (
    MeasuredSpanCategory,
    ReplayFreezeInputChange,
    ReplayLegIdentity,
    ReplayScenarioExpectation,
    ScenarioOutcome,
    ScenarioState,
    SpanCategoryTotals,
    SpanReduction,
)
from agents_remember.certification.replay.scenarios import (
    REPLAY_ACCEPTANCE_SCENARIOS,
    evaluate_all_replay_scenarios,
    evaluate_replay_scenario,
)
from agents_remember.certification.replay.spans import (
    analyze_span_categories,
    category_wall_union_millis,
    gross_wall_union_millis,
)

__all__ = [
    "REPLAY_ACCEPTANCE_SCENARIOS",
    "GateRunMeasurement",
    "MeasuredSpanCategory",
    "PopulationGeneration",
    "ReplayComparabilityReport",
    "ReplayComparisonReport",
    "ReplayFreeze",
    "ReplayFreezeInput",
    "ReplayFreezeInputChange",
    "ReplayLegIdentity",
    "ReplayPopulation",
    "ReplayScenarioExpectation",
    "ReplayStratum",
    "RunMeasurement",
    "ScenarioOutcome",
    "ScenarioState",
    "SpanCategoryTotals",
    "SpanReduction",
    "analyze_span_categories",
    "build_replay_comparison_report",
    "category_wall_union_millis",
    "compare_replay_freezes",
    "compile_replay_freeze",
    "compile_replay_population",
    "evaluate_all_replay_scenarios",
    "evaluate_replay_scenario",
    "freeze_digest",
    "gross_wall_union_millis",
    "measure_replay_run",
    "population_denominator",
    "require_comparable_replay_pair",
]
