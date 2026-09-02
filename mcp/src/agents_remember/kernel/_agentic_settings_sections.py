"""``orchestration`` section parsers: loops, roles, concurrency, expectations,
agent-notifier, spawn, and the quality gate."""

from __future__ import annotations

from agents_remember.kernel._agentic_settings_core import (
    COMPLEXITY_SCALE,
    DEFAULT_AGENT_NOTIFIER_ESCALATION_BUDGET,
    DEFAULT_AGENT_NOTIFIER_INTERVAL_SECONDS,
    DEFAULT_AGENT_NOTIFIER_REDELIVER_BUDGET,
    DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS,
    DEFAULT_EXPECTATION_SLA_SECONDS,
    DEFAULT_LOOP_MAX_ROUNDS,
    DEFAULT_LOOP_PER_LEVEL,
    DEFAULT_RATE_LIMIT_SECONDS,
    DEFAULT_REVIEWER_REUSE,
    KNOWN_AGENT_NOTIFIER_FIELDS,
    KNOWN_CONCURRENCY_FIELDS,
    KNOWN_EXPECTATION_KINDS,
    KNOWN_EXPECTATIONS_FIELDS,
    KNOWN_LOOP_COMPLEXITY_FIELDS,
    KNOWN_LOOP_DEFAULTS_FIELDS,
    KNOWN_LOOP_LEVEL_FIELDS,
    KNOWN_LOOP_LEVELS,
    KNOWN_LOOPS_FIELDS,
    KNOWN_QUALITY_GATE_FIELDS,
    KNOWN_ROLE_KNOB_FIELDS,
    KNOWN_ROLES,
    KNOWN_SPAWN_FIELDS,
    AgenticSettingsError,
    AgentNotifierSettings,
    ConcurrencySettings,
    ExpectationSettings,
    LoopComplexity,
    LoopDefaults,
    LoopSettings,
    QualityGateSettings,
    RoleKnobs,
    _refuse_unknown,
    _require_bool,
    _require_harness_id,
    _require_object,
    _require_positive_int,
    _require_positive_number,
    _require_string,
    _require_string_list,
)
from agents_remember.kernel.primitives.inbox_backoff import (
    MIN_REDELIVERY_INTERVAL_SECONDS,
)


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/src/agents_remember/kernel/_agentic_settings_sections.py:58).
def _parse_loops(raw: object, *, source: str) -> LoopSettings:  # pragma: no cover
    if raw is None:
        return LoopSettings()
    loops = _require_object(raw, "orchestration.loops", source)
    _refuse_unknown(loops, KNOWN_LOOPS_FIELDS, "orchestration.loops", source)
    defaults = _parse_loop_defaults(loops.get("defaults"), source=source)
    per_level = dict(DEFAULT_LOOP_PER_LEVEL)
    per_level.update(
        _parse_loop_levels(
            loops.get("perLevel"), owner="orchestration.loops.perLevel", source=source
        )
    )
    per_master: dict[str, dict[str, str]] = {}
    raw_per_master = loops.get("perMaster")
    if raw_per_master is not None:
        masters = _require_object(raw_per_master, "orchestration.loops.perMaster", source)
        for master, levels in masters.items():
            if not isinstance(master, str) or not master:
                raise AgenticSettingsError(
                    f"orchestration.loops.perMaster keys must be non-empty master "
                    f"task names: {source}"
                )
            per_master[master] = _parse_loop_levels(
                levels,
                owner=f"orchestration.loops.perMaster.{master}",
                source=source,
            )
    return LoopSettings(defaults=defaults, per_level=per_level, per_master=per_master)


def _parse_loop_defaults(raw: object, *, source: str) -> LoopDefaults:
    if raw is None:
        return LoopDefaults()
    defaults = _require_object(raw, "orchestration.loops.defaults", source)
    _refuse_unknown(defaults, KNOWN_LOOP_DEFAULTS_FIELDS, "orchestration.loops.defaults", source)
    max_rounds = DEFAULT_LOOP_MAX_ROUNDS
    if "maxRounds" in defaults:
        max_rounds = _require_positive_int(
            defaults["maxRounds"], "orchestration.loops.defaults.maxRounds", source
        )
    reviewer_reuse = DEFAULT_REVIEWER_REUSE
    if "reviewerReuse" in defaults:
        reviewer_reuse = _require_string(
            defaults["reviewerReuse"],
            "orchestration.loops.defaults.reviewerReuse",
            source,
        )
    complexity = _parse_loop_complexity(defaults.get("complexity"), source=source)
    return LoopDefaults(max_rounds=max_rounds, reviewer_reuse=reviewer_reuse, complexity=complexity)


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/src/agents_remember/kernel/_agentic_settings_sections.py:109).
def _parse_loop_complexity(raw: object, *, source: str) -> LoopComplexity:  # pragma: no cover
    if raw is None:
        return LoopComplexity()
    complexity = _require_object(raw, "orchestration.loops.defaults.complexity", source)
    _refuse_unknown(
        complexity,
        KNOWN_LOOP_COMPLEXITY_FIELDS,
        "orchestration.loops.defaults.complexity",
        source,
    )
    parsed = LoopComplexity()
    values: dict[str, str] = {
        "full_loop_at": parsed.full_loop_at,
        "builder_at": parsed.builder_at,
    }
    for json_key, attr in (("fullLoopAt", "full_loop_at"), ("builderAt", "builder_at")):
        if json_key not in complexity:
            continue
        owner = f"orchestration.loops.defaults.complexity.{json_key}"
        value = _require_string(complexity[json_key], owner, source)
        if value not in COMPLEXITY_SCALE:
            allowed = ", ".join(COMPLEXITY_SCALE)
            raise AgenticSettingsError(
                f"{owner} must be one of: {allowed}; got {value!r}: {source}"
            )
        values[attr] = value
    return LoopComplexity(**values)


def _parse_loop_levels(raw: object, *, owner: str, source: str) -> dict[str, str]:
    if raw is None:
        return {}
    levels = _require_object(raw, owner, source)
    _refuse_unknown(levels, KNOWN_LOOP_LEVELS, owner, source)
    parsed: dict[str, str] = {}
    for level, value in levels.items():
        entry = _require_object(value, f"{owner}.{level}", source)
        _refuse_unknown(entry, KNOWN_LOOP_LEVEL_FIELDS, f"{owner}.{level}", source)
        # Loop postures are model-interpreted doctrine names (scored,
        # seam-required, none, strategist, ...), deliberately not a closed set.
        parsed[level] = _require_string(entry.get("loop"), f"{owner}.{level}.loop", source)
    return parsed


def _parse_roles(
    raw: object,
    *,
    source: str,
    harness_ids: tuple[str, ...] | None,
    owner: str = "orchestration.roles",
) -> dict[str, RoleKnobs]:
    if raw is None:
        return {}
    roles = _require_object(raw, owner, source)
    _refuse_unknown(roles, KNOWN_ROLES, owner, source)
    parsed: dict[str, RoleKnobs] = {}
    for role, value in roles.items():
        knobs = _require_object(value, f"{owner}.{role}", source)
        _refuse_unknown(knobs, KNOWN_ROLE_KNOB_FIELDS, f"{owner}.{role}", source)
        harness = None
        if "harness" in knobs:
            harness = _require_harness_id(
                knobs["harness"],
                f"{owner}.{role}.harness",
                source,
                harness_ids,
            )
        model = None
        if "model" in knobs:
            model = _require_string(knobs["model"], f"{owner}.{role}.model", source)
        effort = None
        if "effort" in knobs:
            # Deliberately a free string here: the native adapter's dynamic catalog validates it at
            # launch; an explicitly mapped non-native harness validates it at dispatch.
            effort = _require_string(knobs["effort"], f"{owner}.{role}.effort", source)
        # The free-form escape hatch (260703-L16): shape-checked, never content-validated.
        launch_args: tuple[str, ...] = ()
        if "launchArgs" in knobs:
            launch_args = _require_string_list(
                knobs["launchArgs"], f"{owner}.{role}.launchArgs", source
            )
        prompt_keywords: tuple[str, ...] = ()
        if "promptKeywords" in knobs:
            prompt_keywords = _require_string_list(
                knobs["promptKeywords"],
                f"{owner}.{role}.promptKeywords",
                source,
            )
        session_commands: tuple[str, ...] = ()
        if "sessionCommands" in knobs:
            session_commands = _require_string_list(
                knobs["sessionCommands"],
                f"{owner}.{role}.sessionCommands",
                source,
            )
        parsed[role] = RoleKnobs(
            harness=harness,
            model=model,
            effort=effort,
            launch_args=launch_args,
            prompt_keywords=prompt_keywords,
            session_commands=session_commands,
        )
    return parsed


def _parse_roles_per_level(
    raw: object, *, source: str, harness_ids: tuple[str, ...] | None
) -> dict[str, dict[str, RoleKnobs]]:
    """``orchestration.rolesPerLevel`` (260703-L16, ruling 2026-07-07T08:15): per-level role knobs.

    ``{leaf|master|portfolio: {role: knob-overrides}}`` -- the level vocabulary matches
    ``loops.perLevel`` so the two per-level families stay congruent; each override deep-merges over
    the flat ``orchestration.roles`` default at dispatch (:meth:`AgenticSettings.resolved_role_knobs`).
    Additive: absent key = no per-level overrides (existing files unchanged).
    """
    if raw is None:
        return {}
    levels = _require_object(raw, "orchestration.rolesPerLevel", source)
    _refuse_unknown(levels, KNOWN_LOOP_LEVELS, "orchestration.rolesPerLevel", source)
    parsed: dict[str, dict[str, RoleKnobs]] = {}
    for level, value in levels.items():
        parsed[level] = _parse_roles(
            value,
            source=source,
            harness_ids=harness_ids,
            owner=f"orchestration.rolesPerLevel.{level}",
        )
    return parsed


def _parse_concurrency(raw: object, *, source: str) -> ConcurrencySettings:
    if raw is None:
        return ConcurrencySettings()
    concurrency = _require_object(raw, "orchestration.concurrency", source)
    _refuse_unknown(concurrency, KNOWN_CONCURRENCY_FIELDS, "orchestration.concurrency", source)
    parsed: dict[str, int | None] = {
        "max_parallel_masters": None,
        "max_parallel_leaves": None,
        "max_sub_agents": None,
    }
    for json_key, attr in (
        ("maxParallelMasters", "max_parallel_masters"),
        ("maxParallelLeaves", "max_parallel_leaves"),
        ("maxSubAgents", "max_sub_agents"),
    ):
        if json_key in concurrency:
            parsed[attr] = _require_positive_int(
                concurrency[json_key], f"orchestration.concurrency.{json_key}", source
            )
    return ConcurrencySettings(**parsed)


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/src/agents_remember/kernel/_agentic_settings_sections.py:262).
def _parse_expectations(raw: object, *, source: str) -> ExpectationSettings:  # pragma: no cover
    """``orchestration.expectations.defaults``: per-kind SLA seconds, hyphenated kind keys
    (matching the expectation-row kind vocabulary), overriding :data:`DEFAULT_EXPECTATION_SLA_SECONDS`
    entry-by-entry -- an omitted kind keeps its documented default."""
    if raw is None:
        return ExpectationSettings()
    block = _require_object(raw, "orchestration.expectations", source)
    _refuse_unknown(block, KNOWN_EXPECTATIONS_FIELDS, "orchestration.expectations", source)
    sla_seconds = dict(DEFAULT_EXPECTATION_SLA_SECONDS)
    raw_defaults = block.get("defaults")
    if raw_defaults is not None:
        defaults = _require_object(raw_defaults, "orchestration.expectations.defaults", source)
        for kind, value in defaults.items():
            if kind not in KNOWN_EXPECTATION_KINDS:
                allowed = ", ".join(sorted(KNOWN_EXPECTATION_KINDS))
                raise AgenticSettingsError(
                    f"orchestration.expectations.defaults key {kind!r} is not a known "
                    f"expectation kind; allowed: {allowed}: {source}"
                )
            owner = f"orchestration.expectations.defaults.{kind}"
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise AgenticSettingsError(
                    f"{owner} must be a positive number of seconds: {source}"
                )
            sla_seconds[kind] = float(value)
    return ExpectationSettings(sla_seconds=sla_seconds)


def _parse_agent_notifier(raw: object, *, source: str) -> AgentNotifierSettings:
    """``orchestration.agentNotifier``: the deterministic sweep's own knobs (R1/R5).

    Absent -> the documented defaults (enabled, 10s interval, 60s staleness cutoff, inbox-store
    redelivery rate limit inherited).
    """
    if raw is None:
        return AgentNotifierSettings()
    block = _require_object(raw, "orchestration.agentNotifier", source)
    _refuse_unknown(block, KNOWN_AGENT_NOTIFIER_FIELDS, "orchestration.agentNotifier", source)
    enabled = (
        _require_bool(block["enabled"], "orchestration.agentNotifier.enabled", source)
        if "enabled" in block
        else True
    )
    interval_seconds = (
        _require_positive_number(
            block["intervalSeconds"], "orchestration.agentNotifier.intervalSeconds", source
        )
        if "intervalSeconds" in block
        else DEFAULT_AGENT_NOTIFIER_INTERVAL_SECONDS
    )
    stale_cutoff_seconds = (
        _require_positive_number(
            block["staleCutoffSeconds"], "orchestration.agentNotifier.staleCutoffSeconds", source
        )
        if "staleCutoffSeconds" in block
        else DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS
    )
    redeliver_rate_limit_seconds = (
        _require_agent_notifier_floor_seconds(
            block["redeliverRateLimitSeconds"],
            "orchestration.agentNotifier.redeliverRateLimitSeconds",
            source,
        )
        if "redeliverRateLimitSeconds" in block
        else None
    )
    signal_cooldown_seconds = (
        _require_agent_notifier_floor_seconds(
            block["signalCooldownSeconds"],
            "orchestration.agentNotifier.signalCooldownSeconds",
            source,
        )
        if "signalCooldownSeconds" in block
        else DEFAULT_RATE_LIMIT_SECONDS
    )
    redeliver_budget = (
        _require_positive_int(
            block["redeliverBudget"], "orchestration.agentNotifier.redeliverBudget", source
        )
        if "redeliverBudget" in block
        else DEFAULT_AGENT_NOTIFIER_REDELIVER_BUDGET
    )
    escalation_budget = (
        _require_positive_int(
            block["escalationBudget"], "orchestration.agentNotifier.escalationBudget", source
        )
        if "escalationBudget" in block
        else DEFAULT_AGENT_NOTIFIER_ESCALATION_BUDGET
    )
    return AgentNotifierSettings(
        enabled=enabled,
        interval_seconds=interval_seconds,
        stale_cutoff_seconds=stale_cutoff_seconds,
        redeliver_rate_limit_seconds=redeliver_rate_limit_seconds,
        signal_cooldown_seconds=signal_cooldown_seconds,
        redeliver_budget=redeliver_budget,
        escalation_budget=escalation_budget,
    )


def _require_agent_notifier_floor_seconds(raw: object, owner: str, source: str) -> float:
    value = _require_positive_number(raw, owner, source)
    if value < MIN_REDELIVERY_INTERVAL_SECONDS:
        raise AgenticSettingsError(
            f"{owner} must be at least {MIN_REDELIVERY_INTERVAL_SECONDS:g} seconds: {source}"
        )
    return value


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/src/agents_remember/kernel/_agentic_settings_sections.py:369).
def _parse_spawn(
    raw: object, *, source: str, harness_ids: tuple[str, ...] | None
) -> str | None:  # pragma: no cover
    if raw is None:
        return None
    spawn = _require_object(raw, "orchestration.spawn", source)
    _refuse_unknown(spawn, KNOWN_SPAWN_FIELDS, "orchestration.spawn", source)
    if "harness" not in spawn:
        return None
    return _require_harness_id(spawn["harness"], "orchestration.spawn.harness", source, harness_ids)


def _parse_quality_gate(raw: object, *, source: str) -> QualityGateSettings:
    """``orchestration.qualityGate``: the full gate's memory cap in bytes.

    Absent -> executor runtime-managed RAM and swap. Executor identity belongs to the
    repository certification profile. Unknown keys fail loud like every other orchestration
    family; a null at the family key is refused by
    :func:`_refuse_null_families` before this parser runs.
    """
    if raw is None:
        return QualityGateSettings()
    block = _require_object(raw, "orchestration.qualityGate", source)
    _refuse_unknown(block, KNOWN_QUALITY_GATE_FIELDS, "orchestration.qualityGate", source)
    memory_cap_bytes = None
    if "memoryCapBytes" in block:
        memory_cap_bytes = _require_positive_int(
            block["memoryCapBytes"],
            "orchestration.qualityGate.memoryCapBytes",
            source,
        )
    return QualityGateSettings(memory_cap_bytes=memory_cap_bytes)
