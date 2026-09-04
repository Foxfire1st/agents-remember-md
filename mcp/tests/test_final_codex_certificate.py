"""Standalone CCR-R14 Gate-4 certificate binding tests.

Covers the one bound Gate-4 certificate: it requires the exact green
Gate-1..3 predecessor certificate identities, a complete terminal
two-fresh-pass run bound to the exact frozen plan and candidate, shared
runtime authority, and green result manifests for both repetitions.  Every
stale, red, incomplete, mismatched, or retried composition refuses before a
certificate can publish.  Fully standalone.
"""

from __future__ import annotations

import pytest
from agents_remember.certification.certificate_models import GateCertificateIdentity
from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.certificate import (
    compile_gate_four_certificate,
)
from agents_remember.errors import CertificationContractError
from test_final_codex_models import (
    CANDIDATE,
    OTHER_CANDIDATE,
    attempt_record,
    fresh_identities,
    gate4_manifest,
    make_draft,
    make_store,
    plan_record,
    publish_run,
    scenario_failure,
    store_codes,
)


def predecessor_identities() -> tuple[GateCertificateIdentity, ...]:
    return tuple(
        GateCertificateIdentity(gate=gate, certificateDigest=("abcdef" + "0" * 58)[:64])
        for gate in (1, 2, 3)
    )


def green_run() -> tuple:
    identities = fresh_identities()
    attempt = attempt_record(identities=identities)
    one = make_draft(
        repetition_number=1,
        identity=identities[0],
        disposition="pass",
        overrides={"manifest": gate4_manifest().model_dump(mode="json")},
    )
    two = make_draft(
        repetition_number=2,
        identity=identities[1],
        disposition="pass",
        overrides={"manifest": gate4_manifest().model_dump(mode="json")},
    )
    return attempt, (one, two)


class FinalCodexCertificateTests:
    def test_green_two_fresh_pass_publishes_bound_certificate(self, tmp_path) -> None:
        store = make_store(tmp_path)
        attempt, drafts = green_run()
        manifest = publish_run(store, attempt=attempt, drafts=drafts)
        certificate = compile_gate_four_certificate(
            plan_record(),
            manifest,
            predecessor_identities(),
            repository_id="sample-repository",
        )
        assert certificate.certificateDigest == content_digest(certificate.semanticEnvelope)
        assert certificate.semanticEnvelope.gate == 4
        assert certificate.semanticEnvelope.candidateIdentity == CANDIDATE
        assert (
            certificate.semanticEnvelope.certificationPlanDigest
            == plan_record().certifyingPlanDigest
        )
        assert certificate.semanticEnvelope.directPredecessors == predecessor_identities()

    def test_red_manifest_cannot_publish(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        one = make_draft(
            repetition_number=1,
            identity=identities[0],
            disposition="fail",
            overrides={
                "manifest": gate4_manifest(red=True).model_dump(mode="json"),
                "failure": scenario_failure().model_dump(mode="json"),
            },
        )
        store.publish_repetition(running, one)
        manifest = store.manifest(CANDIDATE)
        assert manifest is not None and manifest.complete is False
        with pytest.raises(CertificationContractError) as error:
            compile_gate_four_certificate(
                plan_record(),
                manifest,
                predecessor_identities(),
                repository_id="sample-repository",
            )
        assert "final-codex-certificate-run-incomplete" in store_codes(error.value)

    def test_one_pass_one_fail_cannot_compensate(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        one = make_draft(
            repetition_number=1,
            identity=identities[0],
            disposition="fail",
            overrides={
                "manifest": gate4_manifest(red=True).model_dump(mode="json"),
                "failure": scenario_failure().model_dump(mode="json"),
            },
        )
        two = make_draft(
            repetition_number=2,
            identity=identities[1],
            disposition="pass",
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        store.publish_repetition(running, one)
        store.publish_repetition(running, two)
        manifest = store.manifest(CANDIDATE)
        assert manifest is not None and manifest.aggregate == "red"
        with pytest.raises(CertificationContractError) as error:
            compile_gate_four_certificate(
                plan_record(),
                manifest,
                predecessor_identities(),
                repository_id="sample-repository",
            )
        assert "final-codex-certificate-not-two-fresh-pass" in store_codes(error.value)

    def test_candidate_mismatch_refuses(self, tmp_path) -> None:
        store = make_store(tmp_path)
        attempt, drafts = green_run()
        manifest = publish_run(store, attempt=attempt, drafts=drafts)
        foreign_plan = plan_record(candidate_identity=OTHER_CANDIDATE)
        # The manifest binds CANDIDATE; a foreign plan cannot publish for it.
        with pytest.raises(CertificationContractError):
            compile_gate_four_certificate(
                foreign_plan,
                manifest,
                predecessor_identities(),
                repository_id="sample-repository",
            )

    def test_diagnostic_or_partial_predecessors_refuse(self, tmp_path) -> None:
        store = make_store(tmp_path)
        attempt, drafts = green_run()
        manifest = publish_run(store, attempt=attempt, drafts=drafts)
        with pytest.raises(CertificationContractError) as error:
            compile_gate_four_certificate(
                plan_record(),
                manifest,
                (predecessor_identities()[0],),
                repository_id="sample-repository",
            )
        assert "final-codex-predecessor-prefix-invalid" in store_codes(error.value)
