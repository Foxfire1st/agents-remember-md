"""Selected R21 suffixes reopen exact original evidence before any executor starts."""

from __future__ import annotations

from pathlib import Path

from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    InputChangeClass,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_models import (
    GateCertificate,
)
from agents_remember.certification.models import GateResultManifest
from agents_remember.worktrees.modules.quality import clean_executor
from agents_remember.worktrees.modules.quality.execution.models import (
    CodeCertificationExecution,
    RetainedGateExecution,
)
from test_gate_certification_evidence import _arrange, _publish, _record


def selected_execution(tmp_path: Path, first: int = 3, *, report_transform=None):
    """Use real generation publication, result compilation and certificate-store objects."""
    code, group, prepared, profile = _arrange(tmp_path)
    publication, payload = _publish(prepared, profile, transform=report_transform)
    rows = _record(prepared, publication, payload)
    store = prepared.certificate_store()
    certificates = tuple(store.load(GateCertificate, str(row["certificate"])) for row in rows)
    change_classes: dict[int, InputChangeClass] = {
        1: "gate-1-input",
        2: "gate-2-input",
        3: "gate-3-input",
        4: "gate-4-input",
    }
    changes = (
        CertificateInputChange(
            changeClass=change_classes[first], reason="test selected correction"
        ),
    )
    reuse = plan_certificate_reuse(prepared.frozen_run.admission, certificates, changes)
    retained = tuple(
        RetainedGateExecution(
            certificate,
            store.load(GateResultManifest, certificate.semanticEnvelope.resultManifestDigest),
            publication,
        )
        for certificate in certificates[: first - 1]
    )
    selected = CodeCertificationExecution(
        prepared.frozen_run, reuse, changes, certificates, retained
    )
    request = clean_executor.CleanQualityRequest(
        code,
        group,
        "agents-remember",
        Path("mcp/certification-profile-v1.json"),
        "targeted",
        "HEAD",
        execution=selected,
    )
    return selected, request, profile
