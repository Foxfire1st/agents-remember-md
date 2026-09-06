from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from agents_remember.application.worktree_status import worktree_status_packet
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    lifecycle_operation_locator_path,
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.worktree_contract import (
    CleanupStatus,
    ContractError,
    load_contract,
    write_contract,
)
from test_wire_vocabulary_exhaustiveness import (
    _contract,
)


class ContractBoundaryTests(unittest.TestCase):
    """The other half of the same guarantee: what the parser does with a cell it cannot classify.

    A vocabulary the wire model imports is only closed if nothing can smuggle a foreign value
    past the reader -- but "closed" is not the same as "refused". Refusing was tried and was
    worse than the problem: `load_contract` is the single entry point of `worktree_closeout_apply`,
    `worktree_integrate`, `worktree_cleanup`, `worktree_sync`, `worktree_abandon` and
    `worktree_status`, NONE of which catches `ContractError`, so one unreadable token in one
    cell took all six down at once and left the task unreachable. Measured on contracts that
    are legal on disk: a blanked `workflow_kind`, `cleanup: reclaimed-ish`, `closeout status:
    in-progress`, a `cleanup: Pending` with the wrong case.

    So the split is by direction. The READER substitutes the declared fallback, records the raw
    token, and reports it, because a lifecycle that can still be driven is worth more than a
    parse that is proud. The WRITER refuses, because nothing in this package should ever be the
    thing that put an unknown token on disk. A rewrite by any lifecycle tool then heals the
    file, which is the recovery path a developer actually has.
    """

    # One row per contract cell whose vocabulary the wire model imports: the front-matter line
    # as `write_contract` emits it, the edit that puts an off-vocabulary token there, and the
    # `(wire field, raw token, value it must be read as)` the packet has to report.
    #
    # `closeout` and `integration` both spell their cell `  status:`, so the closeout row edits
    # the FIRST occurrence (`_written_with_cell` replaces once) -- which is the closeout block,
    # because `contract_to_text` writes it first.
    OFF_VOCABULARY_CELLS = (
        (
            "workflow_kind: light-task",
            "workflow_kind: master-task",
            ("workflowKind", "master-task", "light-task"),
        ),
        ("memory_mode: internal", "memory_mode: hybrid", ("memoryMode", "hybrid", "internal")),
        (
            "  status: pending-review",
            "  status: awaiting",
            ("humanReviewStatus", "awaiting", "pending-review"),
        ),
        (
            "  status: not-started",
            "  status: in-progress",
            ("closeoutStatus", "in-progress", "not-started"),
        ),
        (
            "  cleanup: pending",
            "  cleanup: reclaimed-ish",
            ("cleanup", "reclaimed-ish", "pending"),
        ),
    )

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.coordination_root = self.root / "coordination"
        self.addCleanup(self._td.cleanup)
        config_path = self.root / "settings.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "coordinationRoot": self.coordination_root.as_posix(),
                    "workspaceRoot": self.root.as_posix(),
                    "repositories": {"r": {}},
                }
            ),
            encoding="utf-8",
        )
        self.config = load_config(config_path)

    def _written(self, **overrides: Any) -> Path:
        contract = _contract(self.coordination_root, **overrides)
        write_contract(contract.contract_path, contract)
        locator = lifecycle_operation_locator_path(
            contract.coordination_root,
            contract.contract_path,
        )
        if not locator.exists():
            publish_new_lifecycle_operation_location(
                contract,
                contract_text=contract.contract_path.read_text(encoding="utf-8"),
            )
        return contract.contract_path

    def _written_with_cell(self, line: str, replacement: str) -> Path:
        path = self._written()
        path.write_text(
            path.read_text(encoding="utf-8").replace(line, replacement, 1), encoding="utf-8"
        )
        return path

    @pytest.mark.integration
    def test_every_vocabulary_cell_degrades_rather_than_stranding_the_task(self) -> None:
        for line, edit, (wire_field, raw, expected) in self.OFF_VOCABULARY_CELLS:
            with self.subTest(cell=edit.strip()):
                summary = worktree_status_packet(self.config, self._written_with_cell(line, edit))
                # `active`, not `invalidContract`: the lifecycle tools can read this file, so
                # the packet says where the task stands rather than only that it is broken.
                self.assertEqual(summary.state, "active")
                self.assertEqual(getattr(summary, wire_field), expected)
                reported = summary.unknownContractCells or []
                self.assertEqual(len(reported), 1)
                self.assertIn(f"{raw!r} read as {expected!r}", reported[0])

    def test_a_rewrite_heals_the_file_and_that_is_the_recovery_path(self) -> None:
        """What a developer does about it: run the lifecycle. Any tool that writes the contract
        regenerates the document from the model, so the substituted value becomes the file's."""
        path = self._written_with_cell("  cleanup: pending", "  cleanup: reclaimed-ish")
        write_contract(path, load_contract(path))
        healed = load_contract(path)
        self.assertEqual(healed.unknown_cells, ())
        self.assertEqual(healed.cleanup, "pending")
        self.assertNotIn("reclaimed-ish", path.read_text(encoding="utf-8"))

    def test_the_writer_refuses_what_the_reader_tolerated(self) -> None:
        """The other direction of the same boundary: nothing here may PUT one on disk.

        Reached only through a deliberate `cast` -- which is the point: the type says this
        cannot happen, and this is what happens when someone overrides the type anyway.
        """
        smuggled = replace(
            _contract(self.coordination_root),
            cleanup=cast(CleanupStatus, "reclaimed-ish"),
        )
        with self.assertRaises(ContractError) as raised:
            write_contract(smuggled.contract_path, smuggled)
        # The detail first, the destination after it: the write gate names the file it was
        # about to put this on, the same way the read gate names the one it read.
        self.assertIn("invalid cleanup: reclaimed-ish", str(raised.exception))
        self.assertIn(f"(in {smuggled.contract_path})", str(raised.exception))

    @pytest.mark.integration
    def test_a_live_contract_projects_onto_the_wire_model(self) -> None:
        """The whole seam end to end, offline: no worktrees, no repo, no remote probe.

        A pre-closeout contract keeps `landing_active` false, so this exercises the real
        `status_payload` -> `WorktreeSummary` projection without touching the network.
        """
        summary = worktree_status_packet(self.config, self._written())
        self.assertEqual(summary.state, "active")
        self.assertEqual(summary.phase, "worktree-started")
        self.assertEqual(summary.nextTool, "worktree_status")
        self.assertEqual(summary.workflowKind, "light-task")
        self.assertEqual(summary.cleanup, "pending")
        self.assertIsNone(summary.unknownContractCells)


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness_boundary.py:477).
