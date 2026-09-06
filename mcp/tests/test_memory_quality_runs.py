"""Bounded registry and canonical memory-quality controller tests."""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.application.memory_quality import controller, runs
from agents_remember.application.memory_scope import MemoryScope, MemoryScopeIdentity
from agents_remember.errors import MemoryCandidatePairError, MemoryCandidatePairFailure
from agents_remember.memory_quality.check import AVAILABLE_CHECKS
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity


def _pair() -> MemoryCandidatePairIdentity:
    return MemoryCandidatePairIdentity(
        repoId="canonical-repo",
        contractPath="/contract",
        contractDigest="9" * 64,
        codeRoot="/code",
        memoryRoot="/memory",
        codeSourceBranch="super",
        codeWorkBranch="ar/leaf",
        codeBaseCommit="a" * 40,
        memorySourceBranch="super",
        memoryWorkBranch="ar/leaf",
        memoryBaseCommit="b" * 40,
        onboardingRoot="/memory/onboarding",
        ledgerPath="/memory/memory.md",
    )


def _identity(
    label: str,
    *,
    repo_id: str = "repo",
    detail_limit: int = 50,
) -> runs.QualityRunIdentity:
    return runs.QualityRunIdentity(
        repo_id=repo_id,
        scope=MemoryScopeIdentity(
            authority="leaf",
            authority_path=f"/scope/{label}",
            code_root=f"/code/{label}",
            onboarding_root=f"/memory/{label}/onboarding",
        ),
        checks=("check",),
        detail_limit=detail_limit,
        publish_curator_report=False,
    )


class MemoryQualityRunRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(runs._registry.clear)

    def _poll_until_settled(self, run_id: str) -> runs.QualityRunSnapshot:
        deadline = time.monotonic() + 5
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = runs.poll_quality_run("repo", run_id)
            if snapshot is not None and snapshot.status != "running":
                return snapshot
            time.sleep(0.01)
        raise AssertionError(f"run {run_id} did not settle: {snapshot}")

    def test_start_poll_completed_failed_and_unknown(self) -> None:
        completed = runs.start_quality_run(_identity("complete"), lambda: {"ok": True})
        assert completed.run_id is not None
        snapshot = self._poll_until_settled(completed.run_id)
        self.assertEqual(snapshot.status, "completed")
        self.assertEqual(snapshot.result, {"ok": True})

        def fail() -> dict[str, object]:
            raise RuntimeError("probe failure")

        failed = runs.start_quality_run(_identity("fail"), fail)
        assert failed.run_id is not None
        failure = self._poll_until_settled(failed.run_id)
        self.assertEqual(failure.status, "failed")
        self.assertIn("probe failure", failure.error or "")
        self.assertIsNone(runs.poll_quality_run("repo", "missing"))

    def test_launch_failure_rolls_back_the_admitted_slot(self) -> None:
        with (
            mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")),
            self.assertRaisesRegex(RuntimeError, "no thread"),
        ):
            runs.start_quality_run(_identity("launch-failure"), lambda: {"ok": True})
        self.assertEqual(runs._registry, {})

    def test_wrong_repository_poll_never_discloses_any_run_state(self) -> None:
        for status in ("running", "completed", "failed"):
            run_id = f"run-{status}"
            runs._registry[run_id] = runs._QualityRun(
                run_id=run_id,
                identity=_identity(status, repo_id="repo-a"),
                status=status,
                completed_at=None if status == "running" else time.monotonic(),
                result={"secret": status} if status == "completed" else None,
                error="secret failure" if status == "failed" else None,
            )
            self.assertIsNone(runs.poll_quality_run("repo-b", run_id))
            self.assertIsNotNone(runs.poll_quality_run("repo-a", run_id))


class MemoryQualityControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(runs._registry.clear)
        self.scope = MemoryScope(
            repo_id="canonical-repo",
            identity=MemoryScopeIdentity(
                authority="leaf",
                authority_path="/canonical/enclosure/contract",
                code_root="/code",
                onboarding_root="/memory/onboarding",
            ),
            code_root=Path("/code"),
            onboarding_root=Path("/memory/onboarding"),
            context=mock.Mock(),
            curator_report_path=Path("/enclosure/reports/curator-memory-quality.md"),
        )
        pair = _pair()
        self.candidate_scope = MemoryScope(
            repo_id="canonical-repo",
            identity=MemoryScopeIdentity(
                authority="leaf",
                authority_path=pair.contractPath,
                code_root=pair.codeRoot,
                onboarding_root=pair.onboardingRoot,
                pair_identity=pair,
            ),
            code_root=Path(pair.codeRoot),
            onboarding_root=Path(pair.onboardingRoot),
            context=mock.Mock(),
            curator_report_path=Path("/enclosure/reports/curator-memory-quality.md"),
            contract=mock.Mock(),
            pair_identity=pair,
        )

    def test_pair_change_during_derived_evidence_refuses_before_curator_publication(self) -> None:
        pair = _pair()
        error = MemoryCandidatePairError(
            "memory-candidate-pair-stale",
            "candidate pair changed while memory quality was running",
            failure=MemoryCandidatePairFailure(
                field="pairIdentity",
                contract_path=pair.contractPath,
                next_action="worktree_sync",
            ),
        )
        execution = controller.MemoryQualityExecution(
            config=mock.Mock(),
            scope=self.candidate_scope,
            checks=tuple(sorted(AVAILABLE_CHECKS)),
            detail_limit=50,
            publish_curator_report=True,
        )
        with (
            mock.patch.object(
                controller,
                "revalidate_memory_candidate_scope",
                side_effect=[self.candidate_scope, self.candidate_scope, error],
            ),
            mock.patch.object(
                controller,
                "run_memory_quality_check",
                return_value={"ok": True, "checks": {}, "findings": []},
            ) as scan,
            mock.patch.object(
                controller,
                "_curator_candidate_inputs",
                return_value=controller._CuratorCandidateInputs("a" * 40, "b" * 40),
            ),
            mock.patch.object(
                controller,
                "check_missing_onboarding",
                return_value=mock.Mock(findings=[]),
            ),
            mock.patch.object(
                controller,
                "build_route_indexes",
                return_value=mock.Mock(stale_indexes=[]),
            ),
            mock.patch.object(
                controller,
                "split_commit_owned_findings",
                return_value=([], []),
            ),
            mock.patch.object(controller, "write_curator_checklist") as publish,
        ):
            result = controller._execute_or_refuse(execution)
        scan.assert_called_once()
        publish.assert_not_called()
        self.assertEqual(result["status"], "scope-refused")
        self.assertEqual(result["pairStatus"], "memory-candidate-pair-stale")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
