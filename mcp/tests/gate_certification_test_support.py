"""CCR-L28 production gate-seam certification records (F01 wiring proof).

Drives the REAL production entry point gate.run_strict_code_quality_gate
(the host function every closeout/integration gate run invokes) against a real
temporary agents-remember checkout with the shipped profile.  The Dagger
subprocess boundary (gate.run_clean_quality) is monkeypatched exactly as
the pinned executor tests do; every other production step -- profile
admission, R22 plan compilation, R21 admission freeze before Gate 1, published
generation decoding, per-gate result publication, and certificate minting +
content-addressed persistence -- executes the real host code.
"""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import tempfile
from pathlib import Path

from agents_remember.certification.certification_lane import compile_certification_lane
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.authority import (
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.certification.repository_profiles.models import ProfileMode
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
    resolve_repository_profile_selection,
)
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.models.test_evidence import _certifying_evidence_from_verified_dagger
from agents_remember.worktrees.modules.quality import certification_records, clean_executor
from agents_remember.worktrees.modules.quality.clean_executor import (
    CleanQualityOutcome,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)

_REPOSITORY_ID = "agents-remember"
_PROFILE_REFERENCE = Path("mcp/certification-profile-v1.json")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _checkout_with_profile(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "records@test.invalid")
    _git(root, "config", "user.name", "records test")
    target = root / "mcp"
    target.mkdir(parents=True, exist_ok=True)
    profile = Path(__file__).resolve().parents[1] / "certification-profile-v1.json"
    (target / "certification-profile-v1.json").write_bytes(profile.read_bytes())
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _lane_for(root: Path, mode: ProfileMode = "targeted"):
    admitted = load_repository_profile(_REPOSITORY_ID, root, _PROFILE_REFERENCE.as_posix())
    candidate_tree = _git(root, "write-tree")
    candidate = CandidateIdentity(kind="git-tree", value=candidate_tree)
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="closeout", mode=mode
    )
    repository_plan = compile_repository_profile_plan(
        admitted.canonical, selection_id=selection.selectionId, candidate_identity=candidate
    )
    lane = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=certification_records._provenance("test"),  # type: ignore[attr-defined]
        memory_rails=gate_five_memory_rails(selection.selectionId),
    )
    return admitted, lane, candidate_tree


def _artifact_paths() -> dict[str, str]:
    source = (
        Path(__file__).resolve().parents[2] / ".dagger/src/agents_remember_quality/rail_bindings.py"
    )
    return runpy.run_path(str(source))["ARTIFACT_FILE_PATHS"]


def _fixture_record(relative: str, exported: Path | None) -> dict[str, object]:
    payload = f"fixture report {relative}\n".encode()
    if exported is not None:
        path = exported / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def _gate_catalog(lane, exported: Path | None = None) -> list[dict[str, object]]:
    """Fixture run payload: every planned applicable rail passes and binds the
    per-rail evidence/artifact records the R11 manifest contract requires."""
    catalog: list[dict[str, object]] = []
    for gate_plan in lane.certificationPlan.gates:
        if gate_plan.gate == 5:
            continue
        rails = []
        for rail in gate_plan.rails:
            evidence_path = f"rail-evidence/{rail.identity.key}.log"
            evidence = [
                {
                    "evidenceId": item.evidenceId,
                    **_fixture_record(evidence_path, exported),
                    "reference": evidence_path,
                }
                for item in rail.evidenceContract
            ]
            artifacts = [
                {
                    "artifactId": item.artifactId,
                    **_fixture_record(_artifact_paths()[item.artifactId], exported),
                    "evidenceRef": _artifact_paths()[item.artifactId],
                }
                for item in rail.outputArtifacts
            ]
            rails.append(
                {
                    "identity": {
                        "railId": rail.identity.railId,
                        "version": rail.identity.version,
                    },
                    "key": rail.identity.key,
                    "gate": gate_plan.gate,
                    "posture": rail.posture,
                    "status": "pass",
                    "exitCode": 0,
                    "evidence": evidence,
                    "artifacts": artifacts,
                }
            )
        catalog.append(
            {
                "gate": gate_plan.gate,
                "applicability": "applicable",
                "started": True,
                "disposition": "green",
                "zeroStart": False,
                "laterGatesZeroStart": False,
                "rails": rails,
                "selectors": [],
            }
        )
    return catalog


def _green_outcome_factory(worktree_group: Path, lane, candidate_tree: str):
    def _outcome(request):
        with tempfile.TemporaryDirectory() as temporary:
            exported = Path(temporary)
            payload = {
                "status": "passed",
                "exitCode": 0,
                "gates": _gate_catalog(lane, exported),
            }
            (exported / "clean-quality-results.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            admitted = load_repository_profile(
                request.repository_id, request.code_worktree, _PROFILE_REFERENCE.as_posix()
            )
            profile_execution = admit_repository_profile_execution(
                admitted,
                purpose="closeout",
                mode=request.mode,
                candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
            )
            clean_executor._publish_reports(  # type: ignore[attr-defined]
                exported,
                request.worktree_group / "reports",
                candidate_tree=candidate_tree,
                profile_execution=profile_execution,
            )
        manifest = load_published_quality_manifest(request.worktree_group / "reports")
        evidence = _certifying_evidence_from_verified_dagger(
            candidate_tree=candidate_tree,
            result_sha256=manifest.require_file(manifest.result_decoder.artifactPath).sha256,
        )
        return CleanQualityOutcome(
            subprocess.CompletedProcess(["dagger"], 0, stdout="passed\n"),
            evidence,
            manifest,
        )

    return _outcome
