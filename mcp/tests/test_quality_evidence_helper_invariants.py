"""Focused proof for quality evidence identity and selector helpers."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from agents_remember_test_support.code_quality import causal_preflight
from agents_remember_test_support.testing import causal_failures, evidence_lifecycle


def _value(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def test_causal_session_finish_publishes_only_from_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = Path("/tmp/causal-report.json")
    monkeypatch.setattr(causal_failures._STATE, "report_path", report_path)
    monkeypatch.setattr(causal_failures, "load_causal_report", lambda _path: {})
    write = mock.Mock()
    monkeypatch.setattr(causal_failures, "write_causal_report", write)
    monkeypatch.setattr(causal_failures._STATE, "blocked_nodes", {"node-b": "cause-b"})
    monkeypatch.setattr(
        causal_failures._STATE,
        "independent_failures",
        {"node-a": {"nodeId": "node-a"}},
    )

    causal_failures.pytest_sessionfinish(_value(config=_value()), 7)
    payload = write.call_args.args[1]
    assert payload == {
        "runtimeEvidence": {
            "pytestExitCode": 7,
            "blockedNodes": [{"nodeId": "node-b", "causeId": "cause-b"}],
            "independentFailures": [{"nodeId": "node-a"}],
        },
        "acceptanceEligible": False,
    }

    write.reset_mock()
    causal_failures.pytest_sessionfinish(_value(config=_value(workerinput={})), 7)
    write.assert_not_called()


def test_evidence_selector_helpers_reject_invalid_source_and_shape(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.py"
    invalid.write_text("def broken(:\n", encoding="utf-8")
    assert not evidence_lifecycle._replacement_selector_exists(invalid, "test_value")
    assert not evidence_lifecycle._replacement_selector_exists(invalid, "")

    function = ast.parse("def value():\n    pass\n").body[0]
    class_node = ast.parse("class Case:\n    def test_value(self):\n        pass\n").body[0]
    assert isinstance(function, ast.FunctionDef)
    assert isinstance(class_node, ast.ClassDef)
    assert not evidence_lifecycle._selector_tail_exists(function, ("value", "nested"))
    assert evidence_lifecycle._selector_tail_exists(class_node, ("Case", "test_value"))


def test_candidate_identity_binds_tree_attempt_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        mock.patch.object(causal_preflight, "_candidate_tree", return_value="a" * 40),
        mock.patch.object(causal_preflight, "_quality_attempt_nonce", return_value="b" * 32),
        mock.patch.object(
            causal_preflight,
            "_environment_identity",
            return_value=({"python": "3.13.15"}, "c" * 64),
        ),
    ):
        identity = causal_preflight.candidate_identity(Path("/tmp/project"))
    assert identity["tree"] == "a" * 40
    assert identity["environmentId"] == "c" * 64
    assert identity["environment"] == {"python": "3.13.15"}
    assert identity["rawInputs"]["qualityAttemptNoncePresent"] is True  # type: ignore[index]

    with mock.patch.object(
        causal_preflight,
        "run_git",
        return_value=_value(returncode=0, stdout=f"{'d' * 40}\n"),
    ):
        assert causal_preflight._candidate_tree(Path("/tmp/project")) == "d" * 40
    monkeypatch.setenv(causal_preflight.QUALITY_ATTEMPT_NONCE_ENV, "e" * 32)
    assert causal_preflight._quality_attempt_nonce() == "e" * 32
    environment, environment_id = causal_preflight._environment_identity()
    assert set(environment) == {"python", "implementation", "platform"}
    assert len(environment_id) == 64
