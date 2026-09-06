from __future__ import annotations

from pathlib import Path

import pytest
from agents_remember.errors import HarnessControlError
from agents_remember.models.conversations.control_wire import (
    ControlIdentity,
    LaunchSpec,
)
from agents_remember.serving.harness_capabilities import (
    CapabilitySnapshot,
    EffortOption,
    LaunchKnobs,
    ModelCapability,
)
from agents_remember.serving.harness_control_factories import (
    BUILTIN_PROTOCOL_HARNESSES,
    harness_launch_knobs,
)
from agents_remember.serving.harness_launch import (
    ResolvedLaunch,
    apply_launch_knobs,
    validate_launch_selection,
    verify_effective_launch,
)

NOW = "2026-07-15T20:00:00+00:00"


def _selection(
    *,
    harness: str = "claude",
    model: str = "claude-fable-5",
    effort: str = "max",
) -> ResolvedLaunch:
    return ResolvedLaunch(harness, model, effort, Path("/workspace"))


def test_pi_requires_the_exact_provider_qualified_catalog_key() -> None:
    snapshot = CapabilitySnapshot(
        models=(
            ModelCapability(
                key="deepseek/deepseek-v4-flash",
                resolved_model="deepseek-v4-flash",
                display_name="DeepSeek V4 Flash",
                supports_effort=True,
                effort_options=(EffortOption("max", "max"),),
                default_effort="max",
            ),
        ),
        selected_model_key=None,
        selected_effort=None,
    )

    with pytest.raises(
        HarnessControlError,
        match=(
            r"must use an exact provider-qualified catalog key; matching alternatives: "
            r"\[deepseek/deepseek-v4-flash\]"
        ),
    ):
        validate_launch_selection(
            _selection(harness="pi", model="deepseek-v4-flash"),
            snapshot,
        )
    selected = validate_launch_selection(
        _selection(harness="pi", model="deepseek/deepseek-v4-flash"),
        snapshot,
    )
    assert selected.key == "deepseek/deepseek-v4-flash"


def _aliased_snapshot(*, selected_model: str | None) -> CapabilitySnapshot:
    """Claude 2.1.216-shape catalog: ``default`` and ``opus[1m]`` alias one resolved model."""

    efforts = (
        EffortOption("low", "low"),
        EffortOption("medium", "medium"),
        EffortOption("high", "high"),
    )
    return CapabilitySnapshot(
        models=(
            ModelCapability(
                key="default",
                resolved_model="claude-opus-4-8[1m]",
                display_name="Default",
                supports_effort=True,
                effort_options=efforts,
                default_effort="medium",
                is_default=True,
            ),
            ModelCapability(
                key="opus[1m]",
                resolved_model="claude-opus-4-8[1m]",
                display_name="Opus 4.8 (1M context)",
                supports_effort=True,
                effort_options=efforts,
                default_effort="medium",
                is_default=False,
            ),
        ),
        selected_model_key=selected_model,
        selected_effort=None,
    )


def test_effective_launch_still_refuses_a_genuinely_different_model() -> None:
    # The resolved-identity acceptance never masks a truly different model: ``other`` is not in the
    # catalog at all, so there is no resolved-model to compare and the strict check still fires.
    with pytest.raises(HarnessControlError, match="running harness reported 'other'"):
        verify_effective_launch(
            _selection(harness="claude", model="opus[1m]", effort="medium"),
            _aliased_snapshot(selected_model="other"),
            require_effort_echo=False,
        )


def test_apply_launch_knobs_preserves_fixed_argv_and_refuses_duplicate_authority() -> None:
    launch = LaunchSpec(
        identity=ControlIdentity("session", "tmux", NOW),
        harness_id="pi",
        cwd=Path("/workspace"),
        argv=("pi", "--no-session"),
        env={"KEEP": "yes"},
    )

    applied = apply_launch_knobs(
        launch,
        LaunchKnobs(
            argv=("--model", "provider/model", "--thinking", "high"),
            env={"ADAPTER": "yes"},
        ),
    )

    assert applied.argv == (
        "pi",
        "--model",
        "provider/model",
        "--thinking",
        "high",
        "--no-session",
    )
    assert applied.env == {"KEEP": "yes", "ADAPTER": "yes"}
    with pytest.raises(HarnessControlError, match=r"adapter-owned option.*--model"):
        apply_launch_knobs(
            LaunchSpec(
                identity=launch.identity,
                harness_id="pi",
                cwd=launch.cwd,
                argv=("pi", "--model=other"),
            ),
            LaunchKnobs(argv=("--model", "provider/model")),
        )


# --- launch-selection refusals, one contract across all three built-in harnesses -----------
#
# `claude_launch_knobs`, `codex_launch_knobs` and `pi_launch_knobs` each open with the same
# two guards, and each spells the resulting knobs differently (Claude and Pi as argv, Codex
# as session config). Driving them through `harness_launch_knobs` -- the registry the
# adapter factory itself uses -- asserts the contract rather than three implementations of
# it, and a fourth harness added to `BUILTIN_PROTOCOL_HARNESSES` is held to it without an
# edit here.
#
# WHY REFUSAL AND NOT NORMALISATION. A padded ` high` is not a typo the launcher may quietly
# fix: the same string is compared against the vendor's echo by `verify_effective_launch`,
# so a selection silently stripped here would be launched as one value and verified as
# another. The guards say so by refusing, and these cases fail if anyone replaces them with
# a `.strip()`.


def _knob_values(knobs: LaunchKnobs) -> set[str]:
    return {*knobs.argv, *(str(value) for value in knobs.session_config.values())}


@pytest.mark.parametrize("harness_id", sorted(BUILTIN_PROTOCOL_HARNESSES))
def test_every_harness_carries_a_clean_selection_into_its_own_launch_vocabulary(
    harness_id: str,
) -> None:
    # The other half of the refusals above: the guards reject exactly the malformed pairs and
    # let a well-formed one through into whichever vocabulary that harness owns.
    knobs = harness_launch_knobs(harness_id, model_key="provider/model", effort="high")

    assert {"provider/model", "high"} <= _knob_values(knobs)
