from __future__ import annotations

import hashlib
import json
import unittest
from typing import cast

from agents_remember.models.lifecycles.operation import IntegrationQualityCertification
from integration_certification_test_support import structural_quality_references


def _result(*, cap: int | None = None) -> dict[str, object]:
    return {
        "required": True,
        "status": "enforced",
        "passed": True,
        "mode": "full",
        "executor": "dagger",
        "diffBase": "b" * 40,
        "memoryCap": ({"capBytes": cap} if cap is not None else None),
        "memoryPolicy": (
            {"mode": "explicit-cap"} if cap is not None else {"mode": "container-host-managed"}
        ),
    }


def _payload(*, cap: int | None = None) -> dict[str, object]:
    result = _result(cap=cap)
    digest = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **structural_quality_references(),
        "completionFingerprint": "f" * 64,
        "codeCommit": "c" * 40,
        "candidateTree": "d" * 40,
        "attestation": {
            "kind": "organizational-master-completion",
            "completionFingerprint": "f" * 64,
            "codeCommit": "c" * 40,
            "candidateTree": "d" * 40,
            "diffBase": "b" * 40,
            "mode": "full",
            "executor": "dagger",
            "memoryCapBytes": "" if cap is None else str(cap),
        },
        "resultSha256": digest,
        "result": result,
    }


def _with_result(payload: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    changed = dict(payload)
    changed["result"] = result
    changed["resultSha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return changed


class OrganizationalCompletionValidationTests(unittest.TestCase):
    def test_quality_certification_accepts_exact_uncapped_and_capped_results(self) -> None:
        self.assertIsInstance(
            IntegrationQualityCertification.model_validate(_payload()),
            IntegrationQualityCertification,
        )
        self.assertIsInstance(
            IntegrationQualityCertification.model_validate(_payload(cap=4096)),
            IntegrationQualityCertification,
        )

    def test_quality_certification_refuses_incomplete_or_inconsistent_attestation(self) -> None:
        incomplete = _payload()
        del incomplete["attestation"]["diffBase"]  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "attestation is incomplete"):
            IntegrationQualityCertification.model_validate(incomplete)

        fields = {
            "kind": "wrong-kind",
            "completionFingerprint": "0" * 64,
            "codeCommit": "0" * 40,
            "candidateTree": "0" * 40,
            "mode": "targeted",
            "executor": "host",
        }
        for field, value in fields.items():
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "attestation is inconsistent"),
            ):
                changed = _payload()
                changed["attestation"][field] = value  # type: ignore[index]
                IntegrationQualityCertification.model_validate(changed)

    def test_quality_certification_refuses_every_nonexact_gate_result(self) -> None:
        fields = {
            "required": False,
            "status": "skipped",
            "passed": False,
            "mode": "targeted",
            "executor": "host",
            "diffBase": "0" * 40,
        }
        for field, value in fields.items():
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "exact full Dagger gate"),
            ):
                payload = _payload()
                result = dict(cast(dict[str, object], payload["result"]))
                result[field] = value
                IntegrationQualityCertification.model_validate(_with_result(payload, result))

    def test_quality_certification_refuses_every_invalid_memory_policy(self) -> None:
        cases = (
            (None, {"memoryPolicy": "invalid"}, "no exact memory policy"),
            (4096, {"memoryPolicy": {"mode": "host"}}, "memory cap does not match"),
            (4096, {"memoryCap": None}, "memory cap does not match"),
            (4096, {"memoryCap": {"capBytes": 8192}}, "memory cap does not match"),
            (None, {"memoryCap": {"capBytes": 4096}}, "memory policy does not match"),
            (None, {"memoryPolicy": {"mode": "host"}}, "memory policy does not match"),
        )
        for cap, changes, reason in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, reason):
                payload = _payload(cap=cap)
                result = {**payload["result"], **changes}  # type: ignore[dict-item]
                IntegrationQualityCertification.model_validate(_with_result(payload, result))

    def test_quality_certification_refuses_a_result_digest_mismatch(self) -> None:
        payload = _payload()
        payload["resultSha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "result digest does not match"):
            IntegrationQualityCertification.model_validate(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
