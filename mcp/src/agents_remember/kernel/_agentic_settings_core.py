"""Typed agentic settings models, constants, and validation primitives.

The settings family (``orchestration.*``) is merged from a global and an optional
repo-local settings file. This module owns the typed models, the fail-loud key
vocabularies, the shared shape/type validators, and the seeded defaults; the
parsers live in responsibility-split siblings and the loader in
:mod:`agents_remember.kernel.agentic_settings`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents_remember.errors import AgentsRememberError
from agents_remember.kernel.harnesses import HARNESSES, Harness
from agents_remember.kernel.primitives.gate_policy import (
    DEFAULT_GATE_POLICY,
    GatePolicy,
)
from agents_remember.kernel.primitives.inbox_backoff import (
    DEFAULT_RATE_LIMIT_SECONDS,
)


class AgenticSettingsError(AgentsRememberError):
    """Raised when an agentic settings file is malformed or carries unknown ``orchestration.*`` keys."""


# The fail-loud key families. Every set names the complete schema of its level;
# anything else raises AgenticSettingsError with the offending file's path.
KNOWN_ORCHESTRATION_FIELDS = frozenset(
    {
        "gateDelegation",
        "loops",
        "roles",
        "rolesPerLevel",
        "concurrency",
        "spawn",
        "harnesses",
        "expectations",
        # Compatibility window (260713-TES-L1): the legacy key is accepted as an explicit
        # alias for ``agentNotifier`` with a loud deprecation, then removed with the window.
        "supervisor",
        "agentNotifier",
        "qualityGate",
    }
)
# One ``orchestration.harnesses.<id>`` entry (260703-L16): define a NEW harness or override a
# builtin's defaults. Schema + worked example: ``docs/reference/harnesses.md`` (THE manual).
KNOWN_HARNESS_ENTRY_FIELDS = frozenset(
    {
        "name",
        "command",
        "argv",
        "modelFlag",
        "effortFlag",
        "effortFlagValues",
        "effortSessionValues",
        "effortSessionCommand",
    }
)
KNOWN_GATE_DELEGATION_FIELDS = frozenset({"policy", "kinds", "requireReviewerVerdictAtSeams"})
KNOWN_GATE_POLICY_KIND_FIELDS = frozenset({"role", "requireReviewerVerdict"})
# Optional hard cap for the full Dagger quality gate. When absent, the container runtime's
# host RAM and swap own pressure response; this scalar remains for constrained lifecycle runs.
KNOWN_QUALITY_GATE_FIELDS = frozenset({"memoryCapBytes"})
KNOWN_LOOPS_FIELDS = frozenset({"defaults", "perLevel", "perMaster"})
KNOWN_LOOP_DEFAULTS_FIELDS = frozenset({"maxRounds", "reviewerReuse", "complexity"})
KNOWN_LOOP_COMPLEXITY_FIELDS = frozenset({"fullLoopAt", "builderAt"})
KNOWN_LOOP_LEVELS = frozenset({"leaf", "master", "portfolio"})
KNOWN_LOOP_LEVEL_FIELDS = frozenset({"loop"})
# The dispatch-time complexity scale (blast radius x novelty x size) the loop
# thresholds are expressed on (l-01 The Three-Party Loop).
COMPLEXITY_SCALE = ("low", "medium", "high")
# The nine portable role lifecycles the l-01 registry defines.
KNOWN_ROLES = frozenset(
    {
        "architect",
        "orchestrator",
        "designer",
        "strategist",
        "manager",
        "worker",
        "curator",
        "reviewer",
        "system-specialist",
    }
)
KNOWN_ROLE_KNOB_FIELDS = frozenset(
    {"harness", "model", "effort", "launchArgs", "promptKeywords", "sessionCommands"}
)
KNOWN_CONCURRENCY_FIELDS = frozenset({"maxParallelMasters", "maxParallelLeaves", "maxSubAgents"})
KNOWN_SPAWN_FIELDS = frozenset({"harness"})
# R1/R5 (260707-HFX2-L2): the deterministic agent-notifier sweep's own knobs -- interval, enable
# flag, self-liveness staleness cutoff, inbox redelivery rate limit, owner-signal cooldown, and
# the per-sweep load-shed budgets.
KNOWN_AGENT_NOTIFIER_FIELDS = frozenset(
    {
        "enabled",
        "intervalSeconds",
        "staleCutoffSeconds",
        "redeliverRateLimitSeconds",
        "signalCooldownSeconds",
        "redeliverBudget",
        "escalationBudget",
    }
)
DEFAULT_AGENT_NOTIFIER_INTERVAL_SECONDS = 10.0
DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS = 60.0
DEFAULT_AGENT_NOTIFIER_REDELIVER_BUDGET = 1
# CS-6 D1 (260707-HFX2-L12): per-sweep cap on owner-signal emission (seat-liveness +
# dead-upstream), the twin of the redeliver budget. Bounds the synchronous hosted pastes one
# sweep can do; deferred findings re-fire next sweep (level-triggered) so nothing is lost.
DEFAULT_AGENT_NOTIFIER_ESCALATION_BUDGET = 250
# R2 (260707-HFX2-L1): the expectation-row kinds every dispatch surface writes a durable
# what-must-happen-by-when row for, and their default SLAs (schema: docs/reference/settings-json.md,
# Orchestration Expectations). Kept as a plain string set here (not imported from
# controlplane.expectation_rows) to avoid a kernel<->controlplane import cycle; the two must be kept
# in sync -- ``ExpectationKind`` in expectation_rows.py is the sole other definition. The record
# Literal additionally keeps the retired ``ack-by``/``turn-report-by`` values for legacy-row parse
# compatibility; they are not settable here.
KNOWN_EXPECTATION_KINDS = frozenset({"briefed-by", "verdict-by"})
KNOWN_EXPECTATIONS_FIELDS = frozenset({"defaults"})
DEFAULT_EXPECTATION_SLA_SECONDS: dict[str, float] = {
    "briefed-by": 120.0,
    "verdict-by": 1800.0,
}

# The BUILTIN registry ids (claude|codex|pi). Harness references (roles.<role>.harness,
# spawn.harness) are validated against the EFFECTIVE id set -- these plus any
# orchestration.harnesses-defined ids -- so a settings value can never inject argv through a
# reference; argv is definable only through the explicit harnesses family (260703-L16).
HARNESS_IDS = tuple(harness.id for harness in HARNESSES)

# The L12 loop defaults (docs/reference/settings-json.md, Orchestration Loops).
DEFAULT_LOOP_MAX_ROUNDS = 3
DEFAULT_REVIEWER_REUSE = "delta-verify"
DEFAULT_LOOP_PER_LEVEL: dict[str, str] = {
    "leaf": "scored",
    "master": "seam-required",
    "portfolio": "strategist",
}


@dataclass(frozen=True)
class LoopComplexity:
    """The complexity thresholds mapping the dispatch-time score to loop tiers."""

    full_loop_at: str = "high"
    builder_at: str = "medium"


@dataclass(frozen=True)
class LoopDefaults:
    """``orchestration.loops.defaults`` -- round cap, reviewer reuse, tier thresholds."""

    max_rounds: int = DEFAULT_LOOP_MAX_ROUNDS
    reviewer_reuse: str = DEFAULT_REVIEWER_REUSE
    complexity: LoopComplexity = field(default_factory=LoopComplexity)


@dataclass(frozen=True)
class LoopSettings:
    """``orchestration.loops`` -- the three-party-loop knobs (L12 schema).

    ``per_level`` maps level -> loop posture (loop postures are model-interpreted
    doctrine names, validated as non-empty strings, not a closed set);
    ``per_master`` maps a master task name -> sparse per-level overrides.
    The knobs govern the LOOP only: the master-exit SEAM gate is unconditional.
    """

    defaults: LoopDefaults = field(default_factory=LoopDefaults)
    per_level: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LOOP_PER_LEVEL))
    per_master: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RoleKnobs:
    """One role's knob overrides (``orchestration.roles.<role>``); ``None`` = role-file default.

    ``harness`` must be a registry id; ``model``/``effort`` are free strings HERE. Native adapters
    validate them against their token-free dynamic catalog at launch; settings-defined non-native
    harnesses use their explicit registry mappings at dispatch. The
    free-form escape hatch (``launch_args``/``prompt_keywords``/``session_commands``, JSON keys
    ``launchArgs``/``promptKeywords``/``sessionCommands``) is shape-checked only (a list of
    non-empty strings) and NEVER content-validated; the spawn path records it in spawn provenance.
    Empty tuple = not configured.
    """

    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    launch_args: tuple[str, ...] = ()
    prompt_keywords: tuple[str, ...] = ()
    session_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConcurrencySettings:
    """``orchestration.concurrency`` caps; ``None`` = uncapped (the default)."""

    max_parallel_masters: int | None = None
    max_parallel_leaves: int | None = None
    max_sub_agents: int | None = None


@dataclass(frozen=True)
class ExpectationSettings:
    """``orchestration.expectations`` -- owner-visible deadline SLAs (R2, 260707-HFX2-L1).

    The relay never evaluates these rows; they are an owner-visible deadline surface written at
    dispatch-brief (``briefed-by``) and gate open (``verdict-by``), configurable per kind here and
    defaulting to :data:`DEFAULT_EXPECTATION_SLA_SECONDS`.
    """

    sla_seconds: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_EXPECTATION_SLA_SECONDS)
    )

    def sla_for(self, kind: str) -> float:
        return self.sla_seconds.get(kind, DEFAULT_EXPECTATION_SLA_SECONDS[kind])


@dataclass(frozen=True)
class AgentNotifierSettings:
    """``orchestration.agentNotifier`` -- the deterministic sweep's knobs (R1/R5, 260707-HFX2-L2).

    ``redeliver_rate_limit_seconds`` of ``None`` inherits ``OperatorInboxStore.list_redeliverable``'s
    own default. ``signal_cooldown_seconds`` controls agent-notifier pane/seat-liveness signal
    posts. ``redeliver_budget``/``escalation_budget`` are per-sweep load-shed caps: they bound how
    many redeliveries and owner-signal emissions one sweep performs; shed findings re-fire next
    sweep (level-triggered), so nothing is lost.
    """

    enabled: bool = True
    interval_seconds: float = DEFAULT_AGENT_NOTIFIER_INTERVAL_SECONDS
    stale_cutoff_seconds: float = DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS
    redeliver_rate_limit_seconds: float | None = None
    signal_cooldown_seconds: float = DEFAULT_RATE_LIMIT_SECONDS
    redeliver_budget: int = DEFAULT_AGENT_NOTIFIER_REDELIVER_BUDGET
    escalation_budget: int = DEFAULT_AGENT_NOTIFIER_ESCALATION_BUDGET


@dataclass(frozen=True)
class QualityGateSettings:
    """``orchestration.qualityGate`` -- the full quality gate's resource knobs.

    The repository profile owns its executor adapter. ``memory_cap_bytes=None`` leaves
    the full adapter under its runtime's host-managed RAM and swap policy. An explicit
    value is passed through the declared adapter contract. Targeted leaf runs never use it.
    """

    memory_cap_bytes: int | None = None


@dataclass(frozen=True)
class AgenticSettings:
    """The merged, typed agentic settings for one read (global <- local)."""

    gate_policy: GatePolicy = DEFAULT_GATE_POLICY
    require_reviewer_verdict_at_seams: bool = False
    # Whether a settings file (not the default) set gateDelegation -- the
    # boot-snapshot consumer uses this to decide legacy-fallback handling.
    gate_delegation_configured: bool = False
    loops: LoopSettings = field(default_factory=LoopSettings)
    roles: dict[str, RoleKnobs] = field(default_factory=dict)
    # 260703-L16 (ruling 2026-07-07T08:15): per-LEVEL role-knob overrides -- the L12 doctrine's
    # per-level agent sets, expressible at last. ``roles`` stays the flat DEFAULTS; each
    # ``roles_per_level[level][role]`` deep-merges over the flat default at leaf-key granularity
    # (see :meth:`resolved_role_knobs`). Level vocabulary matches ``loops.perLevel``.
    roles_per_level: dict[str, dict[str, RoleKnobs]] = field(default_factory=dict)
    concurrency: ConcurrencySettings = field(default_factory=ConcurrencySettings)
    expectations: ExpectationSettings = field(default_factory=ExpectationSettings)
    agent_notifier: AgentNotifierSettings = field(default_factory=AgentNotifierSettings)
    quality_gate: QualityGateSettings = field(default_factory=QualityGateSettings)
    spawn_harness: str | None = None
    # The EFFECTIVE harness registry (260703-L16): the builtin defaults merged with the
    # ``orchestration.harnesses`` family -- new ids added, builtin ids possibly pre-customized.
    # The spawn path resolves harness ids against THIS set, never the builtin table directly.
    harnesses: tuple[Harness, ...] = HARNESSES
    # The settings files that existed and were merged, global first.
    sources: tuple[Path, ...] = ()

    def role_knobs(self, role: str) -> RoleKnobs:
        return self.roles.get(role, RoleKnobs())

    def resolved_role_knobs(self, role: str, level: str = "leaf") -> RoleKnobs:
        """The effective knobs for ``role`` dispatched at ``level`` (260703-L16).

        The per-level override deep-merges over the flat role default at leaf-key granularity: an
        unset override field inherits the default (harness inherited unless overridden), a set one
        replaces it (the free-form lists REPLACE, never concatenate -- the array rule). Because
        both families were already merged local-over-global per file layer, this realizes the full
        dispatch chain: repo-local level override > global level override > repo-local role
        default > global role default.
        """
        base = self.roles.get(role, RoleKnobs())
        override = self.roles_per_level.get(level, {}).get(role)
        if override is None:
            return base
        return RoleKnobs(
            harness=override.harness or base.harness,
            model=override.model or base.model,
            effort=override.effort or base.effort,
            launch_args=override.launch_args or base.launch_args,
            prompt_keywords=override.prompt_keywords or base.prompt_keywords,
            session_commands=override.session_commands or base.session_commands,
        )

    def find_harness(self, harness_id: str) -> Harness | None:
        """The effective :class:`Harness` for ``harness_id`` (builtin merged with settings)."""
        return next((harness for harness in self.harnesses if harness.id == harness_id), None)


def agentic_settings_path(root: Path) -> Path:
    """The agentic settings file under ``root`` (coordination root or code repo)."""
    return root / "system" / "settings.json"


def default_agentic_settings_seed() -> dict[str, Any]:
    """The seeded global-file content: every agentic knob at its documented default.

    ``runtime_install`` writes this copy-if-missing; the c-13 install interview
    then edits it with the developer. No spawn harness preference is seeded --
    the spawn seam stays detection-gated until a preference is configured.
    """
    return {
        "$comment": (
            "Agentic orchestration settings (GLOBAL layer). Schema: agents-remember "
            "docs/reference/settings-json.md (Agentic Settings). Repo-local overrides: "
            "<code-repo>/system/settings.json. Unknown orchestration.* keys fail loud."
        ),
        "version": 1,
        "orchestration": {
            "gateDelegation": {"policy": "all-human"},
            "loops": {
                "defaults": {
                    "maxRounds": DEFAULT_LOOP_MAX_ROUNDS,
                    "reviewerReuse": DEFAULT_REVIEWER_REUSE,
                    "complexity": {"fullLoopAt": "high", "builderAt": "medium"},
                },
                "perLevel": {
                    level: {"loop": posture} for level, posture in DEFAULT_LOOP_PER_LEVEL.items()
                },
            },
        },
    }


def default_agentic_settings_seed_text() -> str:
    return json.dumps(default_agentic_settings_seed(), indent=2) + "\n"


def merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge at leaf-key granularity: object leaves recurse, everything else
    (scalars AND arrays) is REPLACED by the override."""
    merged = dict(base)
    for key, value in override.items():
        prior = merged.get(key)
        if isinstance(prior, dict) and isinstance(value, dict):
            merged[key] = merge_settings(prior, value)
        else:
            merged[key] = value
    return merged


def _refuse_unknown(raw: dict[str, Any], known: frozenset[str], owner: str, source: str) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        unknown_text = ", ".join(unknown)
        allowed = ", ".join(sorted(known))
        raise AgenticSettingsError(
            f"unsupported {owner} setting(s): {unknown_text}; allowed: {allowed}; "
            f"offending file: {source}"
        )


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/src/agents_remember/kernel/_agentic_settings_core.py:433).
def _require_object(raw: object, owner: str, source: str) -> dict[str, Any]:  # pragma: no cover
    if not isinstance(raw, dict):
        raise AgenticSettingsError(f"{owner} must be an object: {source}")
    return raw


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/src/agents_remember/kernel/_agentic_settings_core.py:439).
def _require_string(raw: object, owner: str, source: str) -> str:  # pragma: no cover
    if not isinstance(raw, str) or not raw:
        raise AgenticSettingsError(f"{owner} must be a non-empty string: {source}")
    return raw


def _require_positive_int(raw: object, owner: str, source: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise AgenticSettingsError(f"{owner} must be a positive integer: {source}")
    return raw


def _require_positive_number(raw: object, owner: str, source: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise AgenticSettingsError(f"{owner} must be a positive number: {source}")
    return float(raw)


def _require_bool(raw: object, owner: str, source: str) -> bool:
    if not isinstance(raw, bool):
        raise AgenticSettingsError(f"{owner} must be a boolean: {source}")
    return raw


def _require_string_list(raw: object, owner: str, source: str) -> tuple[str, ...]:
    """A free-form list of non-empty strings (shape-checked only; content never validated).

    An EMPTY list is refused (PR #100 review, Codex P2): ``RoleKnobs`` cannot distinguish
    "absent" from "explicitly cleared" (empty tuple = not configured), so a per-level ``[]``
    meant to clear a flat default would silently INHERIT it instead — the same silent-degrade
    shape as a ``null`` family key (260703-L18 finding 6). Omit the key to inherit; list
    values to override.
    """
    if not isinstance(raw, list):
        raise AgenticSettingsError(f"{owner} must be a list of non-empty strings: {source}")
    if not raw:
        raise AgenticSettingsError(
            f"{owner} must not be an empty list (omit the key to inherit, or list the values "
            f"to override): {source}"
        )
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise AgenticSettingsError(f"{owner} must be a list of non-empty strings: {source}")
        values.append(item)
    return tuple(values)


def _require_harness_id(
    raw: object, owner: str, source: str, harness_ids: tuple[str, ...] | None
) -> str:
    """A harness reference: shape-checked always, membership-checked against the EFFECTIVE id set.

    ``harness_ids`` is the effective registry (builtin merged with ``orchestration.harnesses``);
    ``None`` skips membership -- the per-file (non-strict) pass, where the other layer may declare
    the id.
    """
    value = _require_string(raw, owner, source)
    if harness_ids is not None and value not in harness_ids:
        allowed = ", ".join(harness_ids)
        raise AgenticSettingsError(
            f"{owner} must be a harness registry id or an orchestration.harnesses-defined id "
            f"({allowed}), got {value!r}: {source} (see docs/reference/harnesses.md)"
        )
    return value
