"""Bounded registry and canonical memory-quality controller tests."""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agents_remember.application import memory_scope as memory_scope_module
from agents_remember.application.memory_quality import controller, runs
from agents_remember.application.memory_scope import MemoryScope, MemoryScopeIdentity
from agents_remember.errors import MemoryCandidatePairError, MemoryCandidatePairFailure
from agents_remember.memory_quality.check import AVAILABLE_CHECKS
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.memory import (
    MemoryQualityPollRequest,
    MemoryQualityStartRequest,
)


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

    def test_saturation_reuses_same_identity_and_refuses_unique_without_launch(self) -> None:
        release = threading.Event()
        started = threading.Event()
        launches = 0

        def work() -> dict[str, object]:
            nonlocal launches
            launches += 1
            started.set()
            release.wait(5)
            return {"ok": True}

        try:
            with mock.patch.object(runs, "MAX_QUALITY_RUNS", 1):
                first = runs.start_quality_run(_identity("same"), work)
                self.assertTrue(started.wait(1))
                same = runs.start_quality_run(_identity("same"), work)
                refused = runs.start_quality_run(_identity("unique"), work)
            self.assertEqual(same.state, "running")
            self.assertEqual(same.run_id, first.run_id)
            self.assertEqual(refused.state, "capacity-reached")
            self.assertIsNone(refused.run_id)
            self.assertEqual(len(runs._registry), 1)
            self.assertEqual(launches, 1)
        finally:
            release.set()

    def test_pruning_removes_expired_then_only_enough_oldest_terminal_rows(self) -> None:
        now = time.monotonic()
        rows = (
            runs._QualityRun(
                run_id="expired",
                identity=_identity("expired"),
                status="completed",
                completed_at=now - runs.QUALITY_RUN_TTL_SECONDS - 1,
            ),
            runs._QualityRun(
                run_id="old",
                identity=_identity("old"),
                status="completed",
                completed_at=now - 20,
            ),
            runs._QualityRun(
                run_id="new",
                identity=_identity("new"),
                status="failed",
                completed_at=now - 10,
            ),
            runs._QualityRun(
                run_id="live",
                identity=_identity("live"),
                status="running",
                completed_at=now - runs.QUALITY_RUN_TTL_SECONDS - 10,
            ),
        )
        runs._registry.update({row.run_id: row for row in rows})
        with mock.patch.object(runs, "MAX_QUALITY_RUNS", 3):
            admission = runs.start_quality_run(_identity("admitted"), lambda: {"ok": True})
        self.assertEqual(admission.state, "started")
        self.assertNotIn("expired", runs._registry)
        self.assertNotIn("old", runs._registry)
        self.assertIn("new", runs._registry)
        self.assertIn("live", runs._registry)
        self.assertLessEqual(len(runs._registry), 3)

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

    def test_concurrent_unique_admission_respects_multiple_capacity_scales(self) -> None:
        for cap in (1, 3):
            with self.subTest(cap=cap):
                runs._registry.clear()
                release = threading.Event()
                callers = cap + 3
                barrier = threading.Barrier(callers)

                def submit(
                    index: int,
                    barrier: threading.Barrier = barrier,
                    release: threading.Event = release,
                ) -> runs.QualityRunAdmission:
                    barrier.wait()
                    return runs.start_quality_run(
                        _identity(f"concurrent-{index}"),
                        lambda: _wait_for_release(release),
                    )

                try:
                    with (
                        mock.patch.object(runs, "MAX_QUALITY_RUNS", cap),
                        ThreadPoolExecutor(max_workers=callers) as pool,
                    ):
                        admissions = list(pool.map(submit, range(callers)))
                    self.assertEqual(
                        sum(item.state == "started" for item in admissions),
                        cap,
                    )
                    self.assertEqual(len(runs._registry), cap)
                finally:
                    release.set()


def _wait_for_release(release: threading.Event) -> dict[str, object]:
    release.wait(5)
    return {"ok": True}


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

    def test_reordered_duplicate_checks_and_path_spellings_share_one_run(self) -> None:
        first_check, second_check = AVAILABLE_CHECKS[:2]
        release = threading.Event()
        started = threading.Event()
        launches = 0

        def execute(_execution: controller.MemoryQualityExecution) -> dict[str, object]:
            nonlocal launches
            launches += 1
            started.set()
            return _wait_for_release(release)

        try:
            with (
                mock.patch.object(
                    controller,
                    "resolve_memory_candidate_scope",
                    return_value=self.candidate_scope,
                ),
                mock.patch.object(controller, "_execute_memory_quality", side_effect=execute),
            ):
                first = controller.start_memory_quality_request(
                    mock.Mock(),
                    MemoryQualityStartRequest(
                        mode="start",
                        repo_id="alias",
                        contract_path="/raw/../contract",
                        checks=[first_check, second_check, first_check],
                    ),
                )
                self.assertTrue(started.wait(1))
                second = controller.start_memory_quality_request(
                    mock.Mock(),
                    MemoryQualityStartRequest(
                        mode="start",
                        repo_id="alias",
                        contract_path="/contract",
                        checks=[second_check, first_check],
                    ),
                )
            self.assertEqual(first["status"], "started")
            self.assertEqual(second["status"], "running")
            self.assertEqual(first["runId"], second["runId"])
            self.assertEqual(first["scopeAuthority"], "leaf-candidate")
            self.assertEqual(first["pairIdentity"], _pair().model_dump(mode="json"))
            self.assertEqual(launches, 1)
        finally:
            release.set()

    def test_each_result_affecting_identity_field_prevents_sharing(self) -> None:
        base = _identity("base")
        variants = (
            base,
            runs.QualityRunIdentity(
                repo_id="other-repo",
                scope=base.scope,
                checks=base.checks,
                detail_limit=base.detail_limit,
                publish_curator_report=base.publish_curator_report,
            ),
            runs.QualityRunIdentity(
                repo_id=base.repo_id,
                scope=MemoryScopeIdentity(
                    authority="official",
                    authority_path="/scope/base",
                    code_root="/code/base",
                    onboarding_root="/memory/base/onboarding",
                ),
                checks=base.checks,
                detail_limit=base.detail_limit,
                publish_curator_report=base.publish_curator_report,
            ),
            runs.QualityRunIdentity(
                repo_id=base.repo_id,
                scope=base.scope,
                checks=("other-check",),
                detail_limit=base.detail_limit,
                publish_curator_report=base.publish_curator_report,
            ),
            runs.QualityRunIdentity(
                repo_id=base.repo_id,
                scope=base.scope,
                checks=base.checks,
                detail_limit=base.detail_limit + 1,
                publish_curator_report=base.publish_curator_report,
            ),
            runs.QualityRunIdentity(
                repo_id=base.repo_id,
                scope=base.scope,
                checks=base.checks,
                detail_limit=base.detail_limit,
                publish_curator_report=True,
            ),
        )
        release = threading.Event()
        try:
            with mock.patch.object(runs, "MAX_QUALITY_RUNS", len(variants)):
                admissions = [
                    runs.start_quality_run(identity, lambda: _wait_for_release(release))
                    for identity in variants
                ]
            self.assertTrue(all(item.state == "started" for item in admissions))
            self.assertEqual(len({item.run_id for item in admissions}), len(variants))
        finally:
            release.set()

    def test_result_affecting_fields_and_curator_publication_change_identity(self) -> None:
        with mock.patch.object(controller, "resolve_memory_scope", return_value=self.scope):
            default = controller._resolve_execution(
                mock.Mock(),
                MemoryQualityStartRequest(mode="start", repo_id="repo"),
            )
            explicit = controller._resolve_execution(
                mock.Mock(),
                MemoryQualityStartRequest(
                    mode="start",
                    repo_id="repo",
                    checks=list(AVAILABLE_CHECKS),
                ),
            )
            detailed = controller._resolve_execution(
                mock.Mock(),
                MemoryQualityStartRequest(
                    mode="start",
                    repo_id="repo",
                    detail_limit=200,
                ),
            )
        self.assertTrue(default.publish_curator_report)
        self.assertFalse(explicit.publish_curator_report)
        self.assertNotEqual(default.identity, explicit.identity)
        self.assertNotEqual(default.identity, detailed.identity)

    def test_capacity_is_a_normal_typed_response_without_run_id(self) -> None:
        execution = controller.MemoryQualityExecution(
            config=mock.Mock(),
            scope=self.scope,
            checks=tuple(sorted(AVAILABLE_CHECKS)),
            detail_limit=50,
            publish_curator_report=True,
        )
        with (
            mock.patch.object(controller, "_resolve_execution", return_value=execution),
            mock.patch.object(
                controller,
                "start_quality_run",
                return_value=runs.QualityRunAdmission(state="capacity-reached"),
            ),
        ):
            result = controller.start_memory_quality_request(
                mock.Mock(),
                MemoryQualityStartRequest(mode="start", repo_id="repo"),
            )
        self.assertEqual(result["status"], "capacity-reached")
        self.assertFalse(result["ok"])
        self.assertNotIn("runId", result)
        self.assertNotIn("8", str(result))

    def test_start_pair_refusal_is_typed_before_registry_admission(self) -> None:
        error = MemoryCandidatePairError(
            "memory-candidate-pair-path-unavailable",
            "memory worktree is gone",
            failure=MemoryCandidatePairFailure(
                field="memoryRoot",
                contract_path="/contract",
            ),
        )
        with (
            mock.patch.object(controller, "_resolve_execution", side_effect=error),
            mock.patch.object(controller, "start_quality_run") as admit,
        ):
            result = controller.start_memory_quality_request(
                mock.Mock(),
                MemoryQualityStartRequest(
                    mode="start",
                    repo_id="canonical-repo",
                    contract_path="/contract",
                ),
            )
        self.assertEqual(result["pairStatus"], error.status)
        admit.assert_not_called()

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

    def test_wrong_repo_and_unknown_poll_have_the_same_non_disclosing_shape(self) -> None:
        runs._registry["known"] = runs._QualityRun(
            run_id="known",
            identity=_identity("known", repo_id="repo-a"),
        )
        with mock.patch.object(
            controller,
            "require_repo",
            side_effect=lambda _config, repo_id: mock.Mock(repo_id=repo_id),
        ):
            wrong = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(mode="poll", repo_id="repo-b", run_id="known"),
            )
            unknown = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(mode="poll", repo_id="repo-b", run_id="missing"),
            )
        self.assertEqual(wrong["status"], "run-not-found")
        self.assertEqual(unknown["status"], "run-not-found")
        self.assertNotIn("secret", str(wrong))
        self.assertEqual(set(wrong) - {"runId"}, set(unknown) - {"runId"})

    def test_candidate_poll_requires_and_revalidates_the_exact_contract_path(self) -> None:
        pair = _pair()
        runs._registry["candidate"] = runs._QualityRun(
            run_id="candidate",
            identity=runs.QualityRunIdentity(
                repo_id="canonical-repo",
                scope=self.candidate_scope.identity,
                checks=("check",),
                detail_limit=50,
                publish_curator_report=False,
            ),
            status="completed",
            completed_at=time.monotonic(),
            result={
                "ok": True,
                "operation": "memory_quality_check",
                "scopeAuthority": "leaf-candidate",
                "acceptanceEligible": True,
                "contractPath": pair.contractPath,
                "pairIdentity": pair.model_dump(mode="json"),
            },
        )
        with (
            mock.patch.object(
                controller,
                "require_repo",
                return_value=mock.Mock(repo_id="canonical-repo"),
            ),
            mock.patch.object(
                controller,
                "resolve_memory_candidate_scope",
                return_value=self.candidate_scope,
            ) as resolve_candidate,
        ):
            omitted = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(
                    mode="poll",
                    repo_id="canonical-repo",
                    run_id="candidate",
                ),
            )
            wrong = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(
                    mode="poll",
                    repo_id="canonical-repo",
                    run_id="candidate",
                    contract_path="/other-contract",
                ),
            )
            exact = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(
                    mode="poll",
                    repo_id="canonical-repo",
                    run_id="candidate",
                    contract_path=pair.contractPath,
                ),
            )
        self.assertEqual(omitted["status"], "scope-refused")
        self.assertEqual(wrong["status"], "scope-refused")
        self.assertEqual(exact["status"], "completed")
        self.assertEqual(exact["pairIdentity"], pair.model_dump(mode="json"))
        resolve_candidate.assert_called_once_with(
            mock.ANY,
            repo_id="canonical-repo",
            contract_path=pair.contractPath,
        )

    def test_candidate_poll_rejects_a_changed_pair_and_preserves_run_identity(self) -> None:
        pair = _pair()
        runs._registry["stale"] = runs._QualityRun(
            run_id="stale",
            identity=runs.QualityRunIdentity(
                repo_id="canonical-repo",
                scope=self.candidate_scope.identity,
                checks=("check",),
                detail_limit=50,
                publish_curator_report=False,
            ),
            status="completed",
            completed_at=time.monotonic(),
            result={"ok": True},
        )
        changed_pair = pair.model_copy(update={"contractDigest": "8" * 64})
        changed_scope = replace(self.candidate_scope, pair_identity=changed_pair)
        with (
            mock.patch.object(
                controller,
                "require_repo",
                return_value=mock.Mock(repo_id="canonical-repo"),
            ),
            mock.patch.object(
                controller,
                "resolve_memory_candidate_scope",
                return_value=changed_scope,
            ),
        ):
            result = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(
                    mode="poll",
                    repo_id="canonical-repo",
                    run_id="stale",
                    contract_path=pair.contractPath,
                ),
            )
        self.assertEqual(result["status"], "scope-refused")
        self.assertEqual(result["pairStatus"], "memory-candidate-pair-stale")
        self.assertEqual(result["runId"], "stale")

    def test_official_running_and_failed_polls_use_total_non_candidate_shapes(self) -> None:
        identity = _identity("official", repo_id="canonical-repo")
        cases: tuple[tuple[str, runs.QualityRunStatus, str | None], ...] = (
            ("running", "running", None),
            ("failed", "failed", "boom"),
        )
        for run_id, status, error in cases:
            runs._registry[run_id] = runs._QualityRun(
                run_id=run_id,
                identity=identity,
                status=status,
                error=error,
            )
        with mock.patch.object(
            controller,
            "require_repo",
            return_value=mock.Mock(repo_id="canonical-repo"),
        ):
            running = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(mode="poll", repo_id="canonical-repo", run_id="running"),
            )
            failed = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(mode="poll", repo_id="canonical-repo", run_id="failed"),
            )
        self.assertEqual(running["status"], "running")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "boom")
        self.assertFalse(running["acceptanceEligible"])

    def test_curator_publication_requires_a_pair_after_final_revalidation(self) -> None:
        payload: dict[str, object] = {"checks": {}, "findings": []}
        with (
            mock.patch.object(controller, "split_commit_owned_findings", return_value=([], [])),
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
                "revalidate_memory_candidate_scope",
                return_value=self.scope,
            ),
            self.assertRaisesRegex(RuntimeError, "no exact code/memory pair identity"),
        ):
            controller._attach_curator_checklist(mock.Mock(), self.scope, payload, {})

    def test_candidate_scope_revalidation_rejects_changed_identity(self) -> None:
        pair = _pair()
        changed_pair = pair.model_copy(update={"contractDigest": "7" * 64})
        changed_scope = replace(self.candidate_scope, pair_identity=changed_pair)
        with (
            mock.patch.object(
                memory_scope_module,
                "resolve_memory_candidate_scope",
                return_value=changed_scope,
            ),
            self.assertRaises(MemoryCandidatePairError) as raised,
        ):
            memory_scope_module.revalidate_memory_candidate_scope(mock.Mock(), self.candidate_scope)
        self.assertEqual(raised.exception.status, "memory-candidate-pair-stale")
        self.assertEqual(
            raised.exception.expected,
            {"pairIdentity": pair.model_dump(mode="json")},
        )

    def test_async_pair_refusal_is_not_relabelled_as_completed(self) -> None:
        pair = _pair()
        runs._registry["refused"] = runs._QualityRun(
            run_id="refused",
            identity=runs.QualityRunIdentity(
                repo_id="canonical-repo",
                scope=self.candidate_scope.identity,
                checks=("check",),
                detail_limit=50,
                publish_curator_report=False,
            ),
            status="completed",
            completed_at=time.monotonic(),
            result={
                "ok": False,
                "operation": "memory_quality_check",
                "repoId": "canonical-repo",
                "status": "scope-refused",
                "pairStatus": "memory-candidate-pair-stale",
                "pairField": "pairIdentity",
                "contractPath": pair.contractPath,
                "detail": "candidate changed during scanning",
                "nextAction": "worktree_sync",
            },
        )
        with (
            mock.patch.object(
                controller,
                "require_repo",
                return_value=mock.Mock(repo_id="canonical-repo"),
            ),
            mock.patch.object(
                controller,
                "resolve_memory_candidate_scope",
                return_value=self.candidate_scope,
            ),
        ):
            result = controller.poll_memory_quality_request(
                mock.Mock(),
                MemoryQualityPollRequest(
                    mode="poll",
                    repo_id="canonical-repo",
                    run_id="refused",
                    contract_path=pair.contractPath,
                ),
            )
        self.assertEqual(result["status"], "scope-refused")
        self.assertEqual(result["pairStatus"], "memory-candidate-pair-stale")

    def test_unknown_checks_refuse_before_scope_resolution_or_registry_admission(self) -> None:
        with (
            mock.patch.object(controller, "resolve_memory_scope") as resolve_scope,
            mock.patch.object(controller, "start_quality_run") as admit,
            self.assertRaisesRegex(ValueError, "unknown memory quality check"),
        ):
            controller.start_memory_quality_request(
                mock.Mock(),
                MemoryQualityStartRequest(
                    mode="start",
                    repo_id="repo",
                    checks=["not-a-check"],
                ),
            )
        resolve_scope.assert_not_called()
        admit.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
