"""Small contract tests for the executable evidence-lane registry."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from agents_remember_test_support.testing.evidence_lanes import (
    EVIDENCE_LANES,
    validate_lane_registry,
)
from agents_remember_test_support.testing.evidence_lifecycle import EvidenceCategory
from agents_remember_test_support.testing.lane_manifest import LaneManifest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = LaneManifest(
    files={
        Path("mcp/tests/test_evidence_lanes.py"): EvidenceCategory.UNIT_REGRESSION,
        Path("mcp/tests/test_active_projector_singleflight.py"): EvidenceCategory.UNIT_REGRESSION,
        Path("mcp/tests/test_conversation_control_api.py"): EvidenceCategory.INTEGRATION,
        Path("mcp/tests/test_pi_rpc_real_smoke.py"): EvidenceCategory.PROVIDER_CONFORMANCE,
        Path("mcp/tests/test_pi_rpc_adapter.py"): EvidenceCategory.PROVIDER_CONFORMANCE,
    },
    overrides=(),
    digest="a" * 64,
)


class _Item:
    def __init__(
        self,
        *markers: str,
        path: Path = REPO_ROOT / "mcp/tests/test_evidence_lanes.py",
    ) -> None:
        self.path = path
        self.nodeid = f"{path.relative_to(REPO_ROOT).as_posix()}::test_example"
        self.markers = set(markers)
        self.user_properties: list[tuple[str, object]] = []

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self.markers else None

    def add_marker(self, marker: str) -> None:
        self.markers.add(marker)


class _Config:
    def __init__(self) -> None:
        self.rootpath = REPO_ROOT
        self.markers: list[str] = []

    def addinivalue_line(self, name: str, line: str) -> None:
        if name != "markers":
            raise AssertionError(f"unexpected ini setting {name}")
        self.markers.append(line)


def _as_item(item: _Item) -> pytest.Item:
    return cast(pytest.Item, item)


class EvidenceLaneRegistryTests(unittest.TestCase):
    def test_incomplete_or_ambiguous_registry_is_refused(self) -> None:
        with self.assertRaisesRegex(pytest.UsageError, "missing categories"):
            validate_lane_registry(EVIDENCE_LANES[:-1])
        with self.assertRaisesRegex(pytest.UsageError, "categories must be unique"):
            validate_lane_registry((*EVIDENCE_LANES, EVIDENCE_LANES[0]))

        duplicate_marker = replace(EVIDENCE_LANES[-1], marker=EVIDENCE_LANES[1].marker)
        with self.assertRaisesRegex(pytest.UsageError, "markers must be unique"):
            validate_lane_registry((*EVIDENCE_LANES[:-1], duplicate_marker))
