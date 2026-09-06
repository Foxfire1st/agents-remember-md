from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agents_remember.tasks import (
    read_task_doc,
)
from agents_remember.worktrees import reopen as reopen_module
from agents_remember.worktrees.reopen import reopen_task
from agents_remember.worktrees.worktree_contract import (
    load_contract,
)
from task_reopen_test_support import (
    _completed_leaf_contract,
    _leaf_doc,
    _master_doc,
)


class ReopenResetTests(unittest.TestCase):
    def test_resets_contract_doc_and_master_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = _completed_leaf_contract(Path(tmp))
            doc_path = _leaf_doc(contract.task_root)
            master_path = _master_doc(contract.task_root)

            result = reopen_task(contract.contract_path)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.payload["state"], "reopened")
            self.assertEqual(result.payload["nextOperation"], "start_reopened_task")
            self.assertEqual(result.payload["nextTool"], "worktree_start")
            next_step = cast("dict[str, object]", result.payload["nextStep"])
            self.assertEqual(next_step["nextTool"], "worktree_start")
            self.assertEqual(next_step["nextArgs"], result.payload["nextArgs"])

            reopened = load_contract(contract.contract_path)
            self.assertEqual(
                (
                    reopened.human_review_status,
                    reopened.approved_for_commit,
                    reopened.closeout_status,
                    reopened.integration_status,
                    reopened.cleanup,
                    reopened.lifecycle_id,
                    reopened.code_commit,
                    reopened.integrated_code_commit,
                ),
                ("pending-review", False, "not-started", "not-started", "reopened", "", "", ""),
            )
            self.assertEqual(reopened.leaf_id, "260698-L1")

            doc = read_task_doc(doc_path)
            self.assertEqual((doc.status, doc.lifecycleId), ("planning", None))
            self.assertTrue(any("reopened" in d.decision for d in doc.decisions))

            master = read_task_doc(master_path)
            self.assertEqual(master.subTasks[0].status, "planning")
            self.assertEqual(master.status, "inProgress")
            self.assertEqual(
                cast("dict[str, object]", result.payload["doc"])["masterIndex"], "reset"
            )

    def test_contract_publish_failure_rolls_back_docs_and_landing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = _completed_leaf_contract(Path(tmp))
            doc_path = _leaf_doc(contract.task_root)
            master_path = _master_doc(contract.task_root)
            landing = contract.contract_path.parent / "landing-final.json"
            landing.write_text('{"finished": true}\n', encoding="utf-8")
            paths = (
                contract.contract_path,
                doc_path,
                doc_path.with_suffix(".md"),
                master_path,
                master_path.with_suffix(".md"),
                landing,
            )
            before = {path: path.read_bytes() for path in paths}

            with mock.patch.object(
                reopen_module, "write_contract", side_effect=OSError("contract locked")
            ):
                result = reopen_task(contract.contract_path)

            self.assertEqual((result.returncode, result.payload["state"]), (2, "blocked"))
            self.assertIn("contract locked", str(result.payload["summary"]))
            self.assertEqual({path: path.read_bytes() for path in paths}, before)


if __name__ == "__main__":
    unittest.main()
