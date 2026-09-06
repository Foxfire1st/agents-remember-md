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
from pathlib import Path
from typing import ClassVar

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel.agentic_settings import (
    AgenticSettingsError,
    RoleKnobs,
    agentic_settings_path,
    load_agentic_settings,
    merge_settings,
)


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

    def test_malformed_json_fails_loud_with_path(self) -> None:
        path = agentic_settings_path(self.coordination_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(
            AgenticSettingsError, "cannot parse agentic settings JSON"
        ) as caught:
            load_agentic_settings(self.coordination_root, self.repo_root)
        self.assertIn(str(path), str(caught.exception))


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

    def test_human_pinned_gate_kind_cannot_be_delegated(self) -> None:
        with self.assertRaisesRegex(AgenticSettingsError, "human-pinned"):
            self._load({"gateDelegation": {"kinds": {"push-approval": {"role": "manager"}}}})


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

    def test_new_id_adds_a_harness_with_defaults_derived(self) -> None:
        settings = self._load({"harnesses": {"hermes": {"command": "hermes"}}})
        hermes = settings.find_harness("hermes")
        assert hermes is not None
        self.assertEqual(hermes.argv, ("hermes",))
        self.assertEqual(hermes.name, "hermes")
        self.assertEqual(hermes.defined_in, "settings")
        # Builtin order is preserved; new ids append.
        self.assertEqual([h.id for h in settings.harnesses], ["claude", "codex", "pi", "hermes"])

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


class QualityGateSettingsTests(unittest.TestCase):
    """``orchestration.qualityGate``: optional hard cap inside the Dagger graph."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        self.coordination_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_executor_authority_is_not_accepted_in_agentic_settings(self) -> None:
        path = write_settings(
            self.coordination_root,
            {"orchestration": {"qualityGate": {"executor": "dagger"}}},
        )

        with self.assertRaisesRegex(AgenticSettingsError, "executor") as caught:
            load_agentic_settings(self.coordination_root)
        self.assertIn(str(path), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
