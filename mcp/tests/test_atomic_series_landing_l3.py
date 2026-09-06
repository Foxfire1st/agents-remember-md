"""L3 forcing for contract/ref-owned atomic landing exclusion."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agents_remember.worktrees.integration import atomic_series_landing as landing
from test_closeout_queue import MASTER_B, QueueFixture


class AtomicSeriesLandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = QueueFixture(self.root, atomic_b=True, memory_mode="internal")
        self.current = self.fixture.contracts[MASTER_B]
        self.series_path = self.fixture.tasks / "master-b" / "series-contract.md"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_declared_parent_same_target_permits_leaf_landing(self) -> None:
        landing.require_atomic_landing_authority(self.current)

    def test_live_nonterminal_unrelated_same_target_contract_blocks_landing(self) -> None:
        unrelated = replace(
            self.current,
            parent_task_name="",
            parent_contract_path=None,
        )
        with self.assertRaises(landing.AtomicLandingBlocked) as raised:
            landing.require_atomic_landing_authority(unrelated)
        self.assertEqual(raised.exception.blocker.state, "live-nonterminal")
