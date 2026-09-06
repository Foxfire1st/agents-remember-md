from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast, get_args

import pytest
from agents_remember.application.worktree_status import worktree_status_packet
from agents_remember.kernel.git_facts import git_facts_to_packet, read_git_facts
from agents_remember.kernel.git_freshness import freshness_to_packet, read_branch_freshness
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.context_packet import BranchFreshness as WireBranchFreshness
from agents_remember.models.context_packet import MemorySummary, RepoSummary
from agents_remember.models.structural.agent import (
    DispatchAgentResponse,
    RenameChildResponse,
    RenameSelfResponse,
    RetireChildResponse,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    lifecycle_operation_locator_path,
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.modules.guidance import recovery_guidance
from agents_remember.worktrees.modules.startup.leaf_ref_start import invalid_contract_request_result
from agents_remember.worktrees.worktree_contract import (
    VALID_MEMORY_MODES,
    CleanupStatus,
    ContractError,
    ContractTask,
    RepoBranchPlan,
    WorkflowKind,
    default_series_contract,
    load_contract,
    write_contract,
)
from test_wire_vocabulary_exhaustiveness import (
    _accepts,
    _contract,
    _module_tree,
    worktree_summary_accepts,
)


class AdvertisedVocabularyTests(unittest.TestCase):
    """The published input contract, held to the published output contract."""

    def test_the_workflow_kinds_advertised_and_declared_are_the_same_set(self) -> None:
        """`worktree_start`'s own docstring names the task formats it accepts -- all of them.

        Asserted EQUAL, in both directions, which is the direction that was missing. Held only
        one way (advertised is a subset of declared), the alias could grow a member no tool
        advertises and no writer emits, and nothing would say so -- which is precisely what had
        happened: `WorkflowKind` carried a bare `chat` and `light` alongside `chat-task` and
        `light-task`, the un-reconciled union of the old hand-written copy and the new one.
        Zero occurrences across the 213 contracts on disk, no production writer, and absent
        from the docstring that is the published input contract. The session-status test below
        pins its own difference explicitly for the same reason; here there is none to pin.
        """
        advertised = _advertised_workflow_kinds()
        self.assertEqual(advertised, set(get_args(WorkflowKind)))
        self.assertEqual(advertised, {"light-task", "chat-task"})
        for kind in sorted(advertised):
            with self.subTest(workflowKind=kind):
                self.assertTrue(worktree_summary_accepts("workflowKind", kind))

    def test_agent_session_tools_are_structural_and_exact_id_tools_are_absent(self) -> None:
        """The agent roster exposes document+role intent, never occupant-id administration."""

        declared = {
            node.name
            for node in ast.walk(_module_tree("mcp/registration/sessions.py"))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(
            {"dispatch_agent", "retire_child", "rename_child", "rename_self"} <= declared
        )
        self.assertEqual(
            declared & {"spawn_agent_session", "session_retire", "session_rename"}, set()
        )

        forbidden = {
            "sessionId",
            "session_id",
            "agentId",
            "agent_id",
            "lifecycleId",
            "lifecycle_id",
            "gateId",
            "gate_id",
        }
        for model in (
            DispatchAgentResponse,
            RetireChildResponse,
            RenameChildResponse,
            RenameSelfResponse,
        ):
            with self.subTest(model=model.__name__):
                fields = {field.alias or name for name, field in model.model_fields.items()}
                self.assertEqual(fields & forbidden, set())

    def test_every_memory_mode_the_contract_accepts_validates_on_both_fields(self) -> None:
        """One packet reports the mode twice; both copies must accept the same set."""
        for mode in sorted(VALID_MEMORY_MODES):
            with self.subTest(memoryMode=mode):
                self.assertTrue(worktree_summary_accepts("memoryMode", mode))
                self.assertTrue(
                    _accepts(
                        MemorySummary,
                        {"storage": {"mode": "internal", "default": "internal"}},
                        "mode",
                        mode,
                    )
                )


class RecoveryGuidanceTests(unittest.TestCase):
    """The other side of the vocabulary split, so the split cannot be undone by accident.

    The gate and block payloads emit the same four keys as the phase machine and must keep
    doing so -- the split is a type-level one, not a wire-level one. What they must *not* do
    is reach `WorktreeSummary`, whose vocabulary would otherwise have to grow to hold "the
    developer must approve this commit" and "the source branch is stale".
    """

    def test_it_emits_the_same_keys_in_the_same_order(self) -> None:
        payload = recovery_guidance(
            "request_commit_approval",
            tool="worktree_closeout_apply",
            args={"contract_path": "/x"},
            required_args=["intent_note"],
        )
        self.assertEqual(
            list(payload),
            ["nextOperation", "nextTool", "nextArgs", "nextRequiredArgs"],
        )

    def test_it_omits_required_args_when_there_are_none(self) -> None:
        payload = recovery_guidance(
            "choose_provider_setup_recovery",
            tool="worktree_start",
            args={"repo_id": "r"},
        )
        self.assertEqual(list(payload), ["nextOperation", "nextTool", "nextArgs"])

    def test_its_vocabulary_is_not_the_wire_vocabulary(self) -> None:
        for operation in ("request_commit_approval", "choose_stale_base_recovery"):
            with self.subTest(nextOperation=operation):
                self.assertFalse(worktree_summary_accepts("nextOperation", operation))
        for tool in ("worktree_start", "worktree_sync"):
            with self.subTest(nextTool=tool):
                self.assertFalse(worktree_summary_accepts("nextTool", tool))


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

    def test_an_unknown_cleanup_cell_degrades_and_names_itself(self) -> None:
        contract = load_contract(
            self._written_with_cell("  cleanup: pending", "  cleanup: reclaimed-ish")
        )
        self.assertEqual(contract.cleanup, "pending")
        self.assertEqual(contract.unknown_cells, ("cleanup='reclaimed-ish' read as 'pending'",))

    def test_a_cell_the_file_leaves_blank_is_the_default_with_nothing_to_report(self) -> None:
        """The six read blank the same way, `workflow_kind` included -- it was the odd one out.

        No `or <default>`, so a cell a developer had emptied was a hard refusal where its five
        siblings normalized. (The limited front-matter parser reads a bare `key:` as the start
        of a section, which is why the value arriving here is `{}` rather than `''`.)
        """
        contract = load_contract(
            self._written_with_cell("workflow_kind: light-task", "workflow_kind:")
        )
        self.assertEqual(contract.workflow_kind, "light-task")
        self.assertEqual(contract.unknown_cells, ())

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

    def test_an_unreadable_memory_mode_degrades_to_the_topology_on_disk(self) -> None:
        """The one cell whose fallback is inferred rather than declared.

        `internal` and `external` decide whether there is a second repository to commit, so
        guessing would make closeout skip work that exists. A recorded memory worktree IS an
        external topology, and that is what the degrade reads.
        """
        external = _contract(
            self.coordination_root,
            memory_mode="external",
            memory_repo_path=self.root / "memory",
            memory_worktree=self.root / "wt" / "memory",
            ledger_path=self.root / "wt" / "memory" / "memory.md",
        )
        write_contract(external.contract_path, external)
        external.contract_path.write_text(
            external.contract_path.read_text(encoding="utf-8").replace(
                "memory_mode: external", "memory_mode: hybrid", 1
            ),
            encoding="utf-8",
        )
        contract = load_contract(external.contract_path)
        self.assertEqual(contract.memory_mode, "external")
        self.assertEqual(contract.unknown_cells, ("memory_mode='hybrid' read as 'external'",))

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

    def test_a_document_that_is_not_a_contract_is_still_the_invalid_contract_state(self) -> None:
        """Tolerance is for a cell's VALUE. A file with no schema has nothing to act on."""
        path = self._written_with_cell(
            "schema: ar-series-contract/v1", "schema: ar-series-contract/v99"
        )
        summary = worktree_status_packet(self.config, path)
        self.assertEqual(summary.state, "invalidContract")
        self.assertEqual(summary.status, "operation-contract-publication-lost")
        self.assertTrue(summary.developerDecisionRequired)
        self.assertEqual(summary.nextAction, "developer-decision")
        assert summary.errorEvidence is not None
        self.assertEqual(summary.errorEvidence["stage"], "contract-read")
        self.assertEqual(summary.errorEvidence["errorType"], "ContractError")
        self.assertNotIn("unsupported series contract schema", str(summary.model_dump()))

    def test_every_refusal_names_the_contract_it_was_reading(self) -> None:
        """The one thing an operator needs from a refusal, on every cell that can provoke one.

        `load_contract` is the single entry point of `worktree_closeout_apply`,
        `worktree_integrate`, `worktree_cleanup`, `worktree_sync`, `worktree_abandon` and
        `worktree_status`, and none of them catches `ContractError`, so whichever tool was run,
        this message is the whole of what comes back. "contract missing required fields:
        task_name" named no file, which on a tasks tree of series and enclosure contracts --
        all of them called `series-contract.md` -- is a search rather than an answer.

        Each row is a legal contract with one front-matter cell emptied or made unreadable, the
        way a hand edit leaves one.
        """
        root = self.coordination_root.as_posix()
        code_worktree = (self.coordination_root / "worktrees" / "r" / "t" / "code").as_posix()
        empty = "required contract path field is empty"
        for line, edit, refusal in (
            ("task_name: t", "task_name:", "contract missing required fields: task_name"),
            ("kind: leaf", "kind: sapling", "invalid contract kind: sapling"),
            ("  leaf_id: l", "  leaf_id:", "leaf contract missing leaf_id"),
            # `repo_path` and `worktree` exist under both `code:` and `memory:`, so these two
            # name the section as well -- a bare key would not say which line to open.
            (f"  root: {root}", "  root:", f"{empty}: coordination.root"),
            (f"  worktree: {code_worktree}", "  worktree:", f"{empty}: code.worktree"),
        ):
            with self.subTest(cell=edit.strip()):
                path = self._written_with_cell(line, edit)
                with self.assertRaises(ContractError) as raised:
                    load_contract(path)
                self.assertIn(refusal, str(raised.exception))
                self.assertIn(f"(in {path})", str(raised.exception))

    def test_a_file_that_is_not_a_contract_at_all_is_named_too(self) -> None:
        """The two whole-file refusals. Their subject IS the file, so it ends the sentence.

        They used to print the `series-contract.md` constant -- the name the workflow writes,
        not the path the reader was handed, which told a developer nothing they did not know.
        """
        for text, refusal in (
            ("not a contract\n", "must start with a front matter block"),
            ("---\nschema: ar-series-contract/v1\n", "front matter is not closed"),
        ):
            with self.subTest(refusal=refusal):
                path = self._written()
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ContractError) as raised:
                    load_contract(path)
                self.assertIn(f"worktree contract {refusal}: {path}", str(raised.exception))

    def test_an_external_memory_contract_with_no_ledger_names_the_file(self) -> None:
        """The last refusal on the read path, and the one furthest from the cell it is about."""
        external = _contract(
            self.coordination_root,
            memory_mode="external",
            memory_repo_path=self.root / "memory",
            memory_worktree=self.coordination_root / "worktrees" / "r" / "t" / "memory",
            ledger_path=(self.coordination_root / "worktrees" / "r" / "t" / "memory" / "memory.md"),
        )
        write_contract(external.contract_path, external)
        path = external.contract_path
        ledger = (
            self.coordination_root / "worktrees" / "r" / "t" / "memory" / "memory.md"
        ).as_posix()
        path.write_text(
            path.read_text(encoding="utf-8").replace(f"  ledger: {ledger}", "  ledger:", 1),
            encoding="utf-8",
        )
        with self.assertRaises(ContractError) as raised:
            load_contract(path)
        self.assertEqual(
            str(raised.exception), f"external-memory contract missing ledger_path (in {path})"
        )

    def test_the_invalid_contract_payload_carries_the_file_to_open(self) -> None:
        """Where an operator actually meets this: `worktree_status`, not a traceback.

        `status.py` puts `str(error)` on the payload verbatim, so the raiser is the only place
        the path had to be added for every tool to gain it -- and the packet already carries
        `contractPath`, but the two are not the same claim: that field is the file the tool was
        pointed at, and the message is the file the refusal is about.
        """
        path = self._written_with_cell("task_name: t", "task_name:")
        summary = worktree_status_packet(self.config, path)
        self.assertEqual(summary.state, "invalidContract")
        self.assertEqual(summary.status, "operation-contract-publication-lost")
        self.assertTrue(summary.developerDecisionRequired)
        self.assertEqual(summary.nextAction, "developer-decision")
        assert summary.errorEvidence is not None
        self.assertEqual(summary.errorEvidence["name"], path.name)
        self.assertNotIn("contract missing required fields: task_name", str(summary.model_dump()))

    def test_an_unknown_workflow_kind_request_is_refused(self) -> None:
        """A *request* is not a legacy file: refuse it before it is written down."""
        task = ContractTask(
            name="t",
            repo_name="r",
            coordination_root=self.coordination_root,
            workflow_kind="master-task",
            memory_mode="internal",
        )
        with self.assertRaises(ContractError) as raised:
            default_series_contract(task, code=RepoBranchPlan(repo_path=self.root / "repo"))
        self.assertIn("workflow_kind must be one of", str(raised.exception))

    def test_a_refused_start_request_is_a_result_not_an_exception(self) -> None:
        """...and `worktree_start`'s handler has no `except ContractError` either."""
        refused = invalid_contract_request_result(ContractError("workflow_kind must be one of []"))
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.payload["state"], "invalid-request")
        self.assertIn("workflow_kind must be one of", str(refused.payload["summary"]))

    def test_a_configured_missing_contract_requires_locator_adoption(self) -> None:
        missing = self.coordination_root / "tasks" / "r" / "missing" / "series-contract.md"
        summary = worktree_status_packet(self.config, missing)
        self.assertEqual(summary.state, "missingContract")
        self.assertEqual(summary.status, "operation-location-adoption-required")
        self.assertTrue(summary.developerDecisionRequired)
        self.assertEqual(summary.nextAction, "developer-decision")
        self.assertEqual(
            summary.expected,
            {
                "contractPath": missing.as_posix(),
                "route": "locator -> root manifest -> root journal",
            },
        )
        observed = summary.observed
        assert observed is not None
        self.assertEqual(observed["state"], "missing")

    def test_no_contract_path_is_the_inactive_state(self) -> None:
        self.assertEqual(worktree_status_packet(self.config, None).state, "inactive")

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

    @pytest.mark.integration
    def test_a_healthy_contract_omits_the_next_required_args_it_has_none_of(self) -> None:
        """The declared wire shape, pinned so it cannot move again unannounced.

        Measured across the 213 contracts on disk: 48 responses that carried
        `"nextRequiredArgs": []` now omit the key. That is the decision, not an accident. The
        projection reports what `next_guidance` said and invents nothing -- the same rule that
        stopped it inventing a `nextTool` of `""` -- and an absent `nextRequiredArgs` means what
        `[]` meant, that the next call needs nothing beyond `nextArgs`. There is no third state
        for it to be confused with.
        """
        body = worktree_status_packet(self.config, self._written()).model_dump(
            mode="json", exclude_none=True
        )
        self.assertIn("nextTool", body)  # there IS a next move...
        self.assertIn("nextArgs", body)
        self.assertNotIn("nextRequiredArgs", body)  # ...and it asks nothing of the caller
        done = worktree_status_packet(self.config, self._written(cleanup="completed")).model_dump(
            mode="json", exclude_none=True
        )
        self.assertEqual({"nextTool", "nextArgs", "nextRequiredArgs"} & set(done), set())


class ProducerWireCrossingTests(unittest.TestCase):
    """The git readers' own degrade paths, projected onto the packet models as production does.

    `GuidanceWalkTests` does this for the phase machine; these are the same idea for the two
    ``kernel`` readers, and they stay offline the same way: a path that does not exist is
    answered before any subprocess runs, and the freshness read is told not to fetch. The
    projection under test is the untyped ``*_to_packet`` dict -- the seam where a state the
    reader invented would have become a ValidationError rather than a type error.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_an_absent_repo_crosses_the_wire_as_unavailable(self) -> None:
        summary = RepoSummary.model_validate(
            git_facts_to_packet(read_git_facts("r", self.root / "nope"))
        )
        self.assertEqual(summary.state, "unavailable")
        self.assertIn("does not exist", summary.error or "")

    def test_a_directory_that_is_not_a_repo_crosses_the_freshness_wire(self) -> None:
        freshness = WireBranchFreshness.model_validate(
            freshness_to_packet(read_branch_freshness(self.root, fetch=False))
        )
        self.assertIn(freshness.state, {"unavailable", "no-branch"})
        self.assertFalse(freshness.fetched)


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness_boundary.py:477).
def _advertised_workflow_kinds() -> set[str]:  # pragma: no cover
    tree = _module_tree("mcp/registration/worktrees.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "worktree_start":
            return set(re.findall(r"'([a-z-]+-task)'", ast.get_docstring(node) or ""))
    raise AssertionError("worktree_start tool declaration not found")
