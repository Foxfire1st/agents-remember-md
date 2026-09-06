"""Exact publication consumption and live certificate evidence retention."""

from __future__ import annotations

import json
from pathlib import Path

from agents_remember.certification.frozen_run.models import freeze_certification_run
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.worktrees.modules.quality import (
    certification_evidence,
    certification_records,
    clean_executor,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)
from gate_certification_test_support import (
    _checkout_with_profile,
    _gate_catalog,
    _lane_for,
)


def _arrange(tmp_path: Path):
    code = _checkout_with_profile(tmp_path / "code")
    admitted, lane, candidate = _lane_for(code)
    group = tmp_path / "enclosure"
    prepared = certification_records._persist_admission(
        certification_records.PreparedCertificationRun(
            group, freeze_certification_run(admitted.canonical, lane)
        )
    )
    execution = admit_repository_profile_execution(
        admitted,
        purpose="closeout",
        mode="targeted",
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate),
        source_selection=lane.repositoryPlan.sourceSelection,
    )
    return code, group, prepared, execution


def _publish(prepared, execution, *, nonce="first", bindings=None, transform=None):
    group = prepared.worktree_group
    exported = group.parent / "export"
    exported.mkdir(exist_ok=True)
    payload = {
        "status": "passed",
        "exitCode": 0,
        "attemptNonce": nonce,
        "gates": _gate_catalog(prepared.lane, exported),
    }
    if transform is not None:
        transform(payload)
    (exported / "clean-quality-results.json").write_text(json.dumps(payload))
    clean_executor._publish_reports(
        exported,
        group / "reports",
        candidate_tree=prepared.candidateTree,
        profile_execution=execution,
        bindings=bindings,
    )
    return load_published_quality_manifest(group / "reports"), payload


def _record(prepared, publication, payload):
    result = certification_records.record_published_generation(
        prepared, publication, payload
    ).as_payload()
    assert result["refused"] == []
    return certification_evidence.read_gate_records(prepared.directory)
