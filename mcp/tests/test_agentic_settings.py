"""Tests for the two-layer agentic settings loader (260703-L13).

Covers the merge semantics (global-only, local-only, override, array-replace),
the fail-loud unknown-key rule scoped to ``orchestration.*`` (naming the
offending file; unknown TOP-LEVEL families are tolerated so future families --
contextProviders first in line -- are not foreclosed), absent-file defaults,
malformed-JSON refusal, and the typed models (loops / roles / concurrency /
spawn harness preference / gateDelegation).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import ClassVar

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel.agentic_settings import (
    AgenticSettingsError,
    RoleKnobs,
    agentic_settings_path,
    default_agentic_settings_seed,
    load_agentic_settings,
    merge_settings,
)
from agents_remember.serving.harnesses import invalid_effort_detail, knob_argv


def write_settings(root: Path, data: dict) -> Path:
    path = agentic_settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


class MergePrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        self.repo_root = root / "workspace" / "repo-a"
        self.coordination_root.mkdir(parents=True)
        self.repo_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_global_only(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"spawn": {"harness": "codex"}}},
        )

        settings = load_agentic_settings(self.coordination_root, self.repo_root)

        self.assertEqual(settings.spawn_harness, "codex")
        self.assertEqual(settings.sources, (agentic_settings_path(self.coordination_root),))

    def test_local_only(self) -> None:
        write_settings(
            self.repo_root,
            {"orchestration": {"roles": {"worker": {"harness": "pi"}}}},
        )

        settings = load_agentic_settings(self.coordination_root, self.repo_root)

        self.assertEqual(settings.roles["worker"].harness, "pi")
        self.assertEqual(settings.sources, (agentic_settings_path(self.repo_root),))

    def test_local_leaf_overrides_global_leaf_and_siblings_survive(self) -> None:
        write_settings(
            self.coordination_root,
            {
                "orchestration": {
                    "spawn": {"harness": "codex"},
                    "loops": {"defaults": {"maxRounds": 5, "reviewerReuse": "delta-verify"}},
                    "concurrency": {"maxParallelLeaves": 3},
                }
            },
        )
        write_settings(
            self.repo_root,
            {
                "orchestration": {
                    "spawn": {"harness": "pi"},
                    "loops": {"defaults": {"maxRounds": 2}},
                }
            },
        )

        settings = load_agentic_settings(self.coordination_root, self.repo_root)

        # Leaf-key granularity: the local maxRounds wins while the global
        # reviewerReuse sibling and the untouched concurrency family survive.
        self.assertEqual(settings.spawn_harness, "pi")
        self.assertEqual(settings.loops.defaults.max_rounds, 2)
        self.assertEqual(settings.loops.defaults.reviewer_reuse, "delta-verify")
        self.assertEqual(settings.concurrency.max_parallel_leaves, 3)
        self.assertEqual(
            settings.sources,
            (
                agentic_settings_path(self.coordination_root),
                agentic_settings_path(self.repo_root),
            ),
        )

    def test_arrays_replace_never_concatenate(self) -> None:
        merged = merge_settings(
            {"kinds": {"a": [1, 2, 3]}, "other": [1]},
            {"kinds": {"a": [9]}},
        )

        self.assertEqual(merged, {"kinds": {"a": [9]}, "other": [1]})

    def test_absent_files_mean_documented_defaults(self) -> None:
        settings = load_agentic_settings(self.coordination_root, self.repo_root)

        self.assertIsNone(settings.gate_policy.rule_for("closeout-approval").delegated_role)
        self.assertFalse(settings.gate_delegation_configured)
        self.assertEqual(settings.loops.defaults.max_rounds, 3)
        self.assertEqual(settings.loops.defaults.reviewer_reuse, "delta-verify")
        self.assertEqual(settings.loops.defaults.complexity.full_loop_at, "high")
        self.assertEqual(settings.loops.defaults.complexity.builder_at, "medium")
        self.assertEqual(
            settings.loops.per_level,
            {"leaf": "scored", "master": "seam-required", "portfolio": "strategist"},
        )
        self.assertEqual(settings.loops.per_master, {})
        self.assertEqual(settings.roles, {})
        self.assertIsNone(settings.concurrency.max_parallel_masters)
        self.assertIsNone(settings.concurrency.max_parallel_leaves)
        self.assertIsNone(settings.concurrency.max_sub_agents)
        self.assertIsNone(settings.spawn_harness)
        self.assertIsNone(settings.quality_gate.memory_cap_bytes)
        self.assertEqual(settings.sources, ())

    def test_repo_root_is_optional(self) -> None:
        write_settings(self.coordination_root, {"orchestration": {"spawn": {"harness": "claude"}}})

        settings = load_agentic_settings(self.coordination_root)

        self.assertEqual(settings.spawn_harness, "claude")


class FailLoudTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        self.repo_root = root / "workspace" / "repo-a"
        self.coordination_root.mkdir(parents=True)
        self.repo_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unknown_orchestration_key_names_the_offending_file(self) -> None:
        path = write_settings(self.coordination_root, {"orchestration": {"lopps": {}}})

        with self.assertRaisesRegex(AgenticSettingsError, "lopps") as caught:
            load_agentic_settings(self.coordination_root, self.repo_root)
        self.assertIn(str(path), str(caught.exception))

    def test_unknown_key_in_local_file_names_the_local_file(self) -> None:
        write_settings(self.coordination_root, {"orchestration": {"spawn": {"harness": "codex"}}})
        local_path = write_settings(self.repo_root, {"orchestration": {"spawn": {"harnes": "pi"}}})

        with self.assertRaisesRegex(AgenticSettingsError, "harnes") as caught:
            load_agentic_settings(self.coordination_root, self.repo_root)
        self.assertIn(str(local_path), str(caught.exception))

    def test_local_gate_delegation_is_refused_global_layer_only(self) -> None:
        """L13 review follow-up (L13R-2): a repo-local gateDelegation would validate
        and then silently do nothing (the boot snapshot reads the global file only)
        -- a fail-open shape. The loader refuses it loudly, naming the local file."""
        write_settings(
            self.coordination_root,
            {"orchestration": {"gateDelegation": {"policy": "all-human"}}},
        )
        local_path = write_settings(
            self.repo_root,
            {"orchestration": {"gateDelegation": {"policy": "all-human"}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "global-layer only") as caught:
            load_agentic_settings(self.coordination_root, self.repo_root)
        self.assertIn(str(local_path), str(caught.exception))

    def test_null_at_a_family_key_in_the_local_layer_refuses_not_a_silent_wipe(self) -> None:
        """FINDING 6 (260703-L18, developer-ruled null = REFUSE): a JSON null at a known
        orchestration family key reads as *absent* to every parser and merge_settings REPLACES a
        non-dict, so a repo-local ``"concurrency": null`` would SILENTLY wipe the global caps. Refuse
        it loudly, naming the offending file, with the 'remove the key to inherit the global value'
        guidance -- proving no silent global wipe. Every family the ruling names is covered."""
        write_settings(
            self.coordination_root,
            {"orchestration": {"concurrency": {"maxParallelLeaves": 3}}},
        )
        for family in ("concurrency", "roles", "loops", "spawn", "rolesPerLevel", "harnesses"):
            local_path = write_settings(self.repo_root, {"orchestration": {family: None}})
            with self.assertRaisesRegex(AgenticSettingsError, "inherit the global value") as caught:
                load_agentic_settings(self.coordination_root, self.repo_root)
            self.assertIn(f"orchestration.{family} is null", str(caught.exception))
            self.assertIn(str(local_path), str(caught.exception))

    def test_null_at_a_family_key_in_the_global_layer_also_refuses(self) -> None:
        """The null rule is uniform across BOTH layers (finding 6). A global-layer null refuses too."""
        global_path = write_settings(self.coordination_root, {"orchestration": {"loops": None}})
        with self.assertRaisesRegex(AgenticSettingsError, "orchestration.loops is null") as caught:
            load_agentic_settings(self.coordination_root, self.repo_root)
        self.assertIn(str(global_path), str(caught.exception))

    def test_unknown_top_level_family_is_tolerated_not_parsed(self) -> None:
        """The fail-loud scope is orchestration.* ONLY (gate amendment 2026-07-06).

        A future top-level family in the same file -- contextProviders is
        earmarked to return here -- must not be foreclosed, while a typo'd
        orchestration.* key still fails loud.
        """
        write_settings(
            self.coordination_root,
            {
                "$comment": "header",
                "version": 1,
                "contextProviders": {"enabled": True},
                "onboarding": {"storage": {"mode": "memory-repo"}},
                "orchestration": {"spawn": {"harness": "codex"}},
            },
        )

        settings = load_agentic_settings(self.coordination_root, self.repo_root)
        self.assertEqual(settings.spawn_harness, "codex")

        write_settings(
            self.coordination_root,
            {
                "contextProviders": {"enabled": True},
                "orchestration": {"spwan": {"harness": "codex"}},
            },
        )
        with self.assertRaisesRegex(AgenticSettingsError, "spwan"):
            load_agentic_settings(self.coordination_root, self.repo_root)

    def test_malformed_json_fails_loud_with_path(self) -> None:
        path = agentic_settings_path(self.coordination_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(
            AgenticSettingsError, "cannot parse agentic settings JSON"
        ) as caught:
            load_agentic_settings(self.coordination_root, self.repo_root)
        self.assertIn(str(path), str(caught.exception))

    def test_non_object_root_fails_loud(self) -> None:
        path = agentic_settings_path(self.coordination_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2]", encoding="utf-8")

        with self.assertRaisesRegex(AgenticSettingsError, "must be a JSON object"):
            load_agentic_settings(self.coordination_root)

    def test_unknown_gate_delegation_key_fails_loud(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"gateDelegation": {"polcy": "all-human"}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "polcy"):
            load_agentic_settings(self.coordination_root)

    def test_unknown_loop_defaults_key_fails_loud(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"loops": {"defaults": {"maxRound": 3}}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "maxRound"):
            load_agentic_settings(self.coordination_root)

    def test_unknown_role_name_fails_loud(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"roles": {"wroker": {"harness": "codex"}}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "wroker"):
            load_agentic_settings(self.coordination_root)

    def test_unknown_concurrency_cap_fails_loud(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"concurrency": {"maxParallelWorkers": 4}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "maxParallelWorkers"):
            load_agentic_settings(self.coordination_root)


class TypedModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.coordination_root = Path(self.tmp.name) / "ar-coordination"
        self.coordination_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load(self, orchestration: dict):
        write_settings(self.coordination_root, {"orchestration": orchestration})
        return load_agentic_settings(self.coordination_root)

    def test_parses_the_full_l12_loop_schema(self) -> None:
        settings = self._load(
            {
                "loops": {
                    "defaults": {
                        "maxRounds": 4,
                        "reviewerReuse": "delta-verify",
                        "complexity": {"fullLoopAt": "medium", "builderAt": "low"},
                    },
                    "perLevel": {
                        "leaf": {"loop": "scored"},
                        "master": {"loop": "none"},
                        "portfolio": {"loop": "strategist"},
                    },
                    "perMaster": {
                        "260703_agent-orchestration": {"leaf": {"loop": "builder-verified"}}
                    },
                }
            }
        )

        self.assertEqual(settings.loops.defaults.max_rounds, 4)
        self.assertEqual(settings.loops.defaults.complexity.full_loop_at, "medium")
        self.assertEqual(settings.loops.defaults.complexity.builder_at, "low")
        self.assertEqual(settings.loops.per_level["master"], "none")
        self.assertEqual(
            settings.loops.per_master,
            {"260703_agent-orchestration": {"leaf": "builder-verified"}},
        )

    def test_partial_per_level_keeps_the_other_level_defaults(self) -> None:
        settings = self._load({"loops": {"perLevel": {"master": {"loop": "none"}}}})

        self.assertEqual(settings.loops.per_level["master"], "none")
        self.assertEqual(settings.loops.per_level["leaf"], "scored")
        self.assertEqual(settings.loops.per_level["portfolio"], "strategist")

    def test_max_rounds_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True, "3"):
            with self.assertRaisesRegex(AgenticSettingsError, "maxRounds"):
                self._load({"loops": {"defaults": {"maxRounds": bad}}})

    def test_complexity_thresholds_are_scale_validated(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "fullLoopAt"):
            self._load({"loops": {"defaults": {"complexity": {"fullLoopAt": "extreme"}}}})

    def test_role_knobs_parse_and_default(self) -> None:
        settings = self._load(
            {
                "roles": {
                    "architect": {"harness": "claude", "effort": "high"},
                    "curator": {"harness": "codex", "effort": "medium"},
                    "worker": {"harness": "codex", "model": "gpt-5", "effort": "medium"},
                    "orchestrator": {"effort": "high"},
                    "system-specialist": {"harness": "claude", "model": "fable"},
                }
            }
        )

        self.assertEqual(
            settings.roles["worker"],
            RoleKnobs(harness="codex", model="gpt-5", effort="medium"),
        )
        self.assertEqual(settings.roles["orchestrator"], RoleKnobs(effort="high"))
        self.assertEqual(settings.roles["architect"], RoleKnobs(harness="claude", effort="high"))
        self.assertEqual(settings.roles["curator"], RoleKnobs(harness="codex", effort="medium"))
        self.assertEqual(
            settings.roles["system-specialist"], RoleKnobs(harness="claude", model="fable")
        )
        # Unconfigured roles resolve to empty knobs (role-file defaults apply).
        self.assertEqual(settings.role_knobs("manager"), RoleKnobs())

    def test_role_harness_must_be_a_registry_id(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "harness registry id.*claude-code"):
            self._load({"roles": {"worker": {"harness": "claude-code"}}})

    def test_spawn_harness_must_be_a_registry_id(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "harness registry id"):
            self._load({"spawn": {"harness": "gemini"}})

    def test_concurrency_caps_parse(self) -> None:
        settings = self._load(
            {
                "concurrency": {
                    "maxParallelMasters": 2,
                    "maxParallelLeaves": 3,
                    "maxSubAgents": 4,
                }
            }
        )

        self.assertEqual(settings.concurrency.max_parallel_masters, 2)
        self.assertEqual(settings.concurrency.max_parallel_leaves, 3)
        self.assertEqual(settings.concurrency.max_sub_agents, 4)

    def test_concurrency_caps_must_be_positive_integers(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "maxSubAgents"):
            self._load({"concurrency": {"maxSubAgents": 0}})

    def test_agent_notifier_defaults_when_absent(self) -> None:
        settings = self._load({})

        self.assertTrue(settings.agent_notifier.enabled)
        self.assertEqual(settings.agent_notifier.interval_seconds, 10.0)
        self.assertEqual(settings.agent_notifier.stale_cutoff_seconds, 60.0)
        self.assertIsNone(settings.agent_notifier.redeliver_rate_limit_seconds)
        self.assertEqual(settings.agent_notifier.signal_cooldown_seconds, 900.0)
        self.assertEqual(settings.agent_notifier.redeliver_budget, 1)

    def test_agent_notifier_knobs_parse(self) -> None:
        settings = self._load(
            {
                "agentNotifier": {
                    "enabled": False,
                    "intervalSeconds": 5,
                    "staleCutoffSeconds": 30,
                    "redeliverRateLimitSeconds": 900,
                    "signalCooldownSeconds": 1200,
                    "redeliverBudget": 75,
                }
            }
        )

        self.assertFalse(settings.agent_notifier.enabled)
        self.assertEqual(settings.agent_notifier.interval_seconds, 5.0)
        self.assertEqual(settings.agent_notifier.stale_cutoff_seconds, 30.0)
        self.assertEqual(settings.agent_notifier.redeliver_rate_limit_seconds, 900.0)
        self.assertEqual(settings.agent_notifier.signal_cooldown_seconds, 1200.0)
        self.assertEqual(settings.agent_notifier.redeliver_budget, 75)

    def test_agent_notifier_enabled_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "agentNotifier.enabled"):
            self._load({"agentNotifier": {"enabled": "yes"}})

    def test_agent_notifier_interval_must_be_positive(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "intervalSeconds"):
            self._load({"agentNotifier": {"intervalSeconds": 0}})

    def test_agent_notifier_redeliver_budget_must_be_positive(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "redeliverBudget"):
            self._load({"agentNotifier": {"redeliverBudget": 0}})

    def test_agent_notifier_redelivery_floor_must_be_at_least_15_minutes(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "redeliverRateLimitSeconds"):
            self._load({"agentNotifier": {"redeliverRateLimitSeconds": 899}})

    def test_agent_notifier_signal_cooldown_must_be_at_least_15_minutes(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "signalCooldownSeconds"):
            self._load({"agentNotifier": {"signalCooldownSeconds": 899}})

    def test_unknown_agent_notifier_key_fails_loud(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "sweepSeconds"):
            self._load({"agentNotifier": {"sweepSeconds": 5}})

    def test_legacy_supervisor_key_is_an_explicit_deprecated_alias(self) -> None:
        # OQ1 (260713-TES-L1): the loader accepts the legacy key during ONE explicit
        # compatibility window, and the alias is loud, never silent.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            settings = self._load(
                {
                    "supervisor": {
                        "enabled": False,
                        "intervalSeconds": 7,
                        "redeliverBudget": 3,
                    }
                }
            )
        self.assertFalse(settings.agent_notifier.enabled)
        self.assertEqual(settings.agent_notifier.interval_seconds, 7.0)
        self.assertEqual(settings.agent_notifier.redeliver_budget, 3)
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, UserWarning)
        self.assertIn("orchestration.supervisor", str(caught[0].message))
        self.assertIn("orchestration.agentNotifier", str(caught[0].message))

    def test_legacy_and_new_agent_notifier_keys_together_fail_loud(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "both set"):
            self._load({"supervisor": {"enabled": True}, "agentNotifier": {"enabled": True}})

    def test_gate_delegation_parses_in_its_new_home(self) -> None:
        settings = self._load(
            {
                "gateDelegation": {
                    "policy": "manager-decides-leaf-gates",
                    "requireReviewerVerdictAtSeams": True,
                }
            }
        )

        self.assertTrue(settings.gate_delegation_configured)
        self.assertTrue(settings.require_reviewer_verdict_at_seams)
        policy = settings.gate_policy
        self.assertEqual(policy.rule_for("plan-approval").delegated_role, "manager")
        handover = policy.rule_for("master-handover-approval")
        self.assertEqual(handover.delegated_role, "orchestrator")
        self.assertTrue(handover.require_reviewer_verdict)

    def test_human_pinned_gate_kind_cannot_be_delegated(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "human-pinned"):
            self._load({"gateDelegation": {"kinds": {"push-approval": {"role": "manager"}}}})

    def test_unsupported_gate_kind_delegation_is_rejected(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "cannot be delegated"):
            self._load({"gateDelegation": {"kinds": {"agent-question": {"role": "manager"}}}})


class RetiredEscalationSettingsTests(unittest.TestCase):
    """The ``orchestration.escalation`` ladder family is demolished: any file that sets it
    fails loud as an unknown key (fail-loud schema, no silent ignore)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.coordination_root = Path(self.tmp.name) / "ar-coordination"
        self.coordination_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load(self, orchestration: dict):
        write_settings(self.coordination_root, {"orchestration": orchestration})
        return load_agentic_settings(self.coordination_root)

    def test_escalation_family_is_refused_loud(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "escalation"):
            self._load({"escalation": {}})

    def test_respawn_after_rung_is_refused_with_the_family(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "escalation"):
            self._load({"escalation": {"respawnAfterRung": 2}})

    def test_no_escalation_attribute_on_loaded_settings(self) -> None:
        settings = self._load({})
        self.assertFalse(hasattr(settings, "escalation"))


class FreeFormRoleKnobTests(unittest.TestCase):
    """260703-L16: the free-form escape hatch on role knobs -- launchArgs / promptKeywords /
    sessionCommands parse ADDITIVELY (old files unchanged), shape-checked only, never
    content-validated; effort stays a free string at load (per-harness dispatch validation)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.coordination_root = Path(self.tmp.name) / "ar-coordination"
        self.coordination_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load(self, orchestration: dict):
        write_settings(self.coordination_root, {"orchestration": orchestration})
        return load_agentic_settings(self.coordination_root)

    def test_free_form_knobs_parse_additively(self) -> None:
        settings = self._load(
            {
                "roles": {
                    "strategist": {
                        "effort": "ultracode",
                        "launchArgs": ["--dangerously-skip-permissions"],
                        "promptKeywords": ["ultracode"],
                        "sessionCommands": ["/statusline off"],
                    }
                }
            }
        )
        self.assertEqual(
            settings.roles["strategist"],
            RoleKnobs(
                effort="ultracode",
                launch_args=("--dangerously-skip-permissions",),
                prompt_keywords=("ultracode",),
                session_commands=("/statusline off",),
            ),
        )

    def test_old_files_without_the_new_fields_are_unchanged(self) -> None:
        settings = self._load({"roles": {"worker": {"harness": "codex"}}})
        knobs = settings.roles["worker"]
        self.assertEqual(knobs.launch_args, ())
        self.assertEqual(knobs.prompt_keywords, ())
        self.assertEqual(knobs.session_commands, ())

    def test_empty_free_form_list_is_refused(self) -> None:
        # PR #100 review (Codex P2): RoleKnobs cannot distinguish absent from cleared (empty
        # tuple = not configured), so "launchArgs": [] would silently INHERIT a flat default
        # instead of clearing it — refused with inherit guidance, the null-family ruling's shape.
        with self.assertRaises(AgenticSettingsError) as raised:
            self._load({"roles": {"worker": {"launchArgs": []}}})
        self.assertIn("must not be an empty list", str(raised.exception))
        self.assertIn("omit the key to inherit", str(raised.exception))

    def test_empty_per_level_list_override_is_refused(self) -> None:
        # The per-level variant of the same trap: [] at a level meant to clear the flat default
        # would inherit it through the field-wise `or` merge in resolved_role_knobs.
        with self.assertRaises(AgenticSettingsError) as raised:
            self._load(
                {
                    "roles": {"worker": {"launchArgs": ["--x"]}},
                    "rolesPerLevel": {"leaf": {"worker": {"sessionCommands": []}}},
                }
            )
        self.assertIn("must not be an empty list", str(raised.exception))

    def test_effort_is_a_free_string_at_load_time(self) -> None:
        # The per-harness vocabulary refusal happens at DISPATCH (the harness is only known then);
        # the loader accepts any non-empty string -- the developer's ultracode file boots.
        settings = self._load({"roles": {"strategist": {"effort": "ultracode"}}})
        self.assertEqual(settings.roles["strategist"].effort, "ultracode")

    def test_free_form_shape_violations_fail_loud(self) -> None:
        for bad in ("--flag", [""], [1], {}):
            with self.assertRaisesRegex(AgenticSettingsError, "launchArgs"):
                self._load({"roles": {"worker": {"launchArgs": bad}}})
        with self.assertRaisesRegex(AgenticSettingsError, "promptKeywords"):
            self._load({"roles": {"worker": {"promptKeywords": "ultracode"}}})
        with self.assertRaisesRegex(AgenticSettingsError, "sessionCommands"):
            self._load({"roles": {"worker": {"sessionCommands": "/effort x"}}})


class RolesPerLevelTests(unittest.TestCase):
    """260703-L16 (ruling 2026-07-07T08:15): orchestration.rolesPerLevel -- per-LEVEL role knob
    overrides deep-merging over the flat orchestration.roles defaults at leaf-key granularity,
    with the loops.perLevel level vocabulary (leaf|master|portfolio)."""

    # The developer's canonical reviewer economics: cheap per leaf, smarter at the master seam,
    # smartest + workflows for the portfolio end-to-end.
    ECONOMICS: ClassVar[dict] = {
        "roles": {"reviewer": {"harness": "claude", "model": "sonnet", "effort": "high"}},
        "rolesPerLevel": {
            "master": {"reviewer": {"model": "opus", "effort": "xhigh"}},
            "portfolio": {"reviewer": {"model": "fable", "effort": "ultracode"}},
        },
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.coordination_root = Path(self.tmp.name) / "ar-coordination"
        self.coordination_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load(self, orchestration: dict):
        write_settings(self.coordination_root, {"orchestration": orchestration})
        return load_agentic_settings(self.coordination_root)

    def test_level_override_deep_merges_over_the_flat_default(self) -> None:
        settings = self._load(self.ECONOMICS)
        # leaf: no override -- the flat default applies untouched.
        self.assertEqual(
            settings.resolved_role_knobs("reviewer", "leaf"),
            RoleKnobs(harness="claude", model="sonnet", effort="high"),
        )
        # master: model/effort overridden, harness INHERITED from the flat default.
        self.assertEqual(
            settings.resolved_role_knobs("reviewer", "master"),
            RoleKnobs(harness="claude", model="opus", effort="xhigh"),
        )
        # portfolio: the smartest set, harness still inherited.
        self.assertEqual(
            settings.resolved_role_knobs("reviewer", "portfolio"),
            RoleKnobs(harness="claude", model="fable", effort="ultracode"),
        )

    def test_default_level_is_leaf(self) -> None:
        settings = self._load(self.ECONOMICS)
        self.assertEqual(
            settings.resolved_role_knobs("reviewer"),
            settings.resolved_role_knobs("reviewer", "leaf"),
        )

    def test_absent_family_changes_nothing(self) -> None:
        settings = self._load({"roles": {"reviewer": {"model": "sonnet"}}})
        self.assertEqual(settings.roles_per_level, {})
        self.assertEqual(
            settings.resolved_role_knobs("reviewer", "master"),
            RoleKnobs(model="sonnet"),
        )

    def test_unknown_level_key_fails_loud(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "epic"):
            self._load({"rolesPerLevel": {"epic": {"reviewer": {"model": "opus"}}}})

    def test_unknown_role_inside_a_level_fails_loud(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "wroker"):
            self._load({"rolesPerLevel": {"master": {"wroker": {"model": "opus"}}}})

    def test_architect_is_allowed_inside_a_level(self) -> None:
        settings = self._load(
            {
                "roles": {"architect": {"harness": "claude", "effort": "high"}},
                "rolesPerLevel": {"portfolio": {"architect": {"model": "opus"}}},
            }
        )

        self.assertEqual(
            settings.resolved_role_knobs("architect", "portfolio"),
            RoleKnobs(harness="claude", model="opus", effort="high"),
        )

    def test_curator_is_allowed_inside_a_level(self) -> None:
        settings = self._load(
            {
                "roles": {"curator": {"harness": "codex", "effort": "medium"}},
                "rolesPerLevel": {"leaf": {"curator": {"model": "gpt-5"}}},
            }
        )

        self.assertEqual(
            settings.resolved_role_knobs("curator", "leaf"),
            RoleKnobs(harness="codex", model="gpt-5", effort="medium"),
        )

    def test_level_override_free_form_lists_replace(self) -> None:
        settings = self._load(
            {
                "roles": {"reviewer": {"promptKeywords": ["careful"]}},
                "rolesPerLevel": {"portfolio": {"reviewer": {"promptKeywords": ["ultracode"]}}},
            }
        )
        self.assertEqual(
            settings.resolved_role_knobs("reviewer", "portfolio").prompt_keywords,
            ("ultracode",),
        )
        self.assertEqual(
            settings.resolved_role_knobs("reviewer", "leaf").prompt_keywords,
            ("careful",),
        )


class HarnessesFamilyTests(unittest.TestCase):
    """260703-L16 registry openness: orchestration.harnesses extends/overrides the builtin registry
    (new ids add, builtin ids pre-customize), with fail-loud shapes, delivery-vehicle pair rules,
    and harness references validated against the EFFECTIVE id set."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        self.repo_root = root / "workspace" / "repo-a"
        self.coordination_root.mkdir(parents=True)
        self.repo_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load(self, orchestration: dict, local: dict | None = None):
        write_settings(self.coordination_root, {"orchestration": orchestration})
        if local is not None:
            write_settings(self.repo_root, {"orchestration": local})
        return load_agentic_settings(
            self.coordination_root, self.repo_root if local is not None else None
        )

    def test_absent_family_means_the_builtin_registry(self) -> None:
        settings = self._load({})
        self.assertEqual([h.id for h in settings.harnesses], ["claude", "codex", "pi"])

    def test_new_id_adds_a_harness_with_defaults_derived(self) -> None:
        settings = self._load({"harnesses": {"hermes": {"command": "hermes"}}})
        hermes = settings.find_harness("hermes")
        assert hermes is not None
        self.assertEqual(hermes.argv, ("hermes",))
        self.assertEqual(hermes.name, "hermes")
        self.assertEqual(hermes.defined_in, "settings")
        # Builtin order is preserved; new ids append.
        self.assertEqual([h.id for h in settings.harnesses], ["claude", "codex", "pi", "hermes"])

    def test_argv_only_entry_derives_its_command(self) -> None:
        settings = self._load({"harnesses": {"hermes": {"argv": ["hermes", "--tui"]}}})
        hermes = settings.find_harness("hermes")
        assert hermes is not None
        self.assertEqual(hermes.command, "hermes")
        self.assertEqual(hermes.argv, ("hermes", "--tui"))

    def test_builtin_override_replaces_fields_and_keeps_the_rest(self) -> None:
        settings = self._load({"harnesses": {"claude": {"argv": ["claude", "--continue"]}}})
        claude = settings.find_harness("claude")
        assert claude is not None
        self.assertEqual(claude.argv, ("claude", "--continue"))
        # Native knob ownership stays out of the static registry; identity survives a partial
        # command override.
        self.assertEqual(claude.command, "claude")
        self.assertIsNone(claude.effort_flag)
        self.assertEqual(claude.defined_in, "registry")

    def test_replacing_codex_effort_flag_drops_its_config_value_template(self) -> None:
        settings = self._load(
            {
                "harnesses": {
                    "codex": {
                        "effortFlag": "--reasoning-effort",
                        "effortFlagValues": ["high", "xhigh"],
                    }
                }
            }
        )
        codex = settings.find_harness("codex")
        assert codex is not None
        self.assertEqual(codex.effort_flag, "--reasoning-effort")
        self.assertIsNone(codex.effort_flag_value_template)
        self.assertEqual(codex.effort_validation, "enumerated")
        self.assertIsNone(invalid_effort_detail(codex, "high"))
        self.assertEqual(knob_argv(codex, effort="high"), ["--reasoning-effort", "high"])
        detail = invalid_effort_detail(codex, "bogus-effort")
        assert detail is not None
        self.assertIn("high, xhigh", detail)
        self.assertEqual(knob_argv(codex, effort="bogus-effort"), [])

    def test_partial_codex_override_does_not_invent_a_static_effort_policy(self) -> None:
        settings = self._load({"harnesses": {"codex": {"argv": ["codex", "--search"]}}})
        codex = settings.find_harness("codex")
        assert codex is not None
        self.assertEqual(codex.effort_validation, "enumerated")
        self.assertEqual(codex.effort_flag_values, ())

    def test_new_id_without_command_or_argv_fails_loud(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "command and/or argv"):
            self._load({"harnesses": {"hermes": {"name": "Hermes"}}})

    def test_unknown_entry_key_fails_loud(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "commandline"):
            self._load({"harnesses": {"hermes": {"commandline": "hermes"}}})

    def test_vocabulary_pairs_must_come_together(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "effortFlagValues"):
            self._load({"harnesses": {"hermes": {"command": "hermes", "effortFlag": "--r"}}})
        with self.assertRaisesRegex(AgenticSettingsError, "effortSessionCommand"):
            self._load(
                {"harnesses": {"hermes": {"command": "hermes", "effortSessionValues": ["ultra"]}}}
            )

    def test_effort_session_command_template_may_reference_only_value(self) -> None:
        """FINDING 4 (260703-L18): effortSessionCommand renders via ``.format(value=…)`` at spawn; a
        stray field (``{mode}``), a positional ``{}``, or an unmatched brace would raise a RAW
        KeyError/ValueError there instead of the structured refusal every other bad knob gets. Refuse
        all of them post-merge, naming the harness -- the raw error at serving/harnesses.py is now
        unreachable from validated settings."""
        for bad in ("/set {mode}={value}", "{}", "/effort {value", "/effort value}"):
            with self.assertRaisesRegex(AgenticSettingsError, "effortSessionCommand"):
                self._load(
                    {
                        "harnesses": {
                            "hermes": {
                                "command": "hermes",
                                "effortSessionValues": ["ultra"],
                                "effortSessionCommand": bad,
                            }
                        }
                    }
                )

    def test_builtin_override_supplying_only_a_bad_command_is_validated(self) -> None:
        """The builtin-override path (finding 4): claude already declares effortSessionValues, so an
        override may supply JUST the command -- which must still be template-validated, naming claude."""
        with self.assertRaisesRegex(AgenticSettingsError, "effortSessionCommand") as caught:
            self._load({"harnesses": {"claude": {"effortSessionCommand": "/set {mode}={value}"}}})
        self.assertIn("orchestration.harnesses.claude", str(caught.exception))

    def test_valid_effort_session_command_is_accepted(self) -> None:
        """A template referencing only ``{value}`` passes (the check is not over-eager)."""
        settings = self._load(
            {
                "harnesses": {
                    "hermes": {
                        "command": "hermes",
                        "effortSessionValues": ["ultra"],
                        "effortSessionCommand": "/effort {value}",
                    }
                }
            }
        )
        hermes = settings.find_harness("hermes")
        assert hermes is not None
        self.assertEqual(hermes.effort_session_command, "/effort {value}")

    def test_role_and_spawn_references_accept_settings_defined_ids(self) -> None:
        settings = self._load(
            {
                "harnesses": {"hermes": {"command": "hermes"}},
                "spawn": {"harness": "hermes"},
                "roles": {"worker": {"harness": "hermes"}},
            }
        )
        self.assertEqual(settings.spawn_harness, "hermes")
        self.assertEqual(settings.roles["worker"].harness, "hermes")

    def test_cross_layer_reference_and_partial_override_merge(self) -> None:
        # The GLOBAL layer declares hermes; the LOCAL layer may reference it and override a single
        # leaf (per-file validation is shape-only; cross-references bind on the MERGED block).
        settings = self._load(
            {"harnesses": {"hermes": {"command": "hermes", "argv": ["hermes", "--global"]}}},
            local={
                "harnesses": {"hermes": {"argv": ["hermes", "--local"]}},
                "spawn": {"harness": "hermes"},
            },
        )
        hermes = settings.find_harness("hermes")
        assert hermes is not None
        self.assertEqual(hermes.argv, ("hermes", "--local"))
        self.assertEqual(settings.spawn_harness, "hermes")

    def test_reference_to_an_id_known_nowhere_fails_naming_the_manual(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "harnesses.md"):
            self._load({"spawn": {"harness": "hermes"}})


class SeedTests(unittest.TestCase):
    def test_seed_parses_to_the_documented_defaults(self) -> None:
        """The runtime_install seed must round-trip through the loader to the
        same posture an ABSENT file yields (all-human gates, L12 loop defaults,
        no caps, no spawn preference)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            coordination_root = Path(tmp_dir) / "ar-coordination"
            coordination_root.mkdir(parents=True)
            write_settings(coordination_root, default_agentic_settings_seed())

            seeded = load_agentic_settings(coordination_root)
            absent = load_agentic_settings(Path(tmp_dir) / "elsewhere")

        self.assertEqual(seeded.gate_policy, absent.gate_policy)
        self.assertEqual(seeded.loops, absent.loops)
        self.assertEqual(seeded.roles, absent.roles)
        self.assertEqual(seeded.concurrency, absent.concurrency)
        self.assertEqual(seeded.quality_gate, absent.quality_gate)
        self.assertIsNone(seeded.spawn_harness)
        # The seed EXPLICITLY configures gateDelegation (all-human) so the
        # boot-snapshot consumer treats the file as the key's home.
        self.assertTrue(seeded.gate_delegation_configured)


class QualityGateSettingsTests(unittest.TestCase):
    """``orchestration.qualityGate``: optional hard cap inside the Dagger graph."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        self.coordination_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults_when_absent(self) -> None:
        settings = load_agentic_settings(self.coordination_root)

        self.assertIsNone(settings.quality_gate.memory_cap_bytes)
        self.assertFalse(hasattr(settings.quality_gate, "executor"))

    def test_memory_cap_bytes_parses_and_overrides(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"qualityGate": {"memoryCapBytes": 1073741824}}},
        )

        settings = load_agentic_settings(self.coordination_root)

        self.assertEqual(settings.quality_gate.memory_cap_bytes, 1073741824)

    def test_an_empty_quality_gate_block_keeps_the_default(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"qualityGate": {}}},
        )

        settings = load_agentic_settings(self.coordination_root)

        self.assertIsNone(settings.quality_gate.memory_cap_bytes)
        self.assertFalse(hasattr(settings.quality_gate, "executor"))

    def test_executor_authority_is_not_accepted_in_agentic_settings(self) -> None:
        path = write_settings(
            self.coordination_root,
            {"orchestration": {"qualityGate": {"executor": "dagger"}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "executor") as caught:
            load_agentic_settings(self.coordination_root)
        self.assertIn(str(path), str(caught.exception))

    def test_unknown_quality_gate_key_fails_loud(self) -> None:
        path = write_settings(
            self.coordination_root,
            {"orchestration": {"qualityGate": {"memoryCapMegabytes": 1}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "memoryCapMegabytes") as caught:
            load_agentic_settings(self.coordination_root)
        self.assertIn(str(path), str(caught.exception))

    def test_memory_cap_must_be_a_positive_integer(self) -> None:
        write_settings(
            self.coordination_root,
            {"orchestration": {"qualityGate": {"memoryCapBytes": 0}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "positive integer"):
            load_agentic_settings(self.coordination_root)


if __name__ == "__main__":
    unittest.main()
