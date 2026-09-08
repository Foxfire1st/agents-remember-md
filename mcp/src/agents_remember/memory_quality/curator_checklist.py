"""Render the curator's one enclosure-local memory-quality worklist.

The checklist is an operational artifact, not onboarding and not a task report.  A full
contract-scoped ``memory_quality_check`` replaces the same file on every run so a curator has
one current list rather than a trail of timestamped snapshots.  Worktree cleanup owns its
lifetime through the enclosure's reserved ``reports`` directory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.kernel.git_command import run_git
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorSourceCandidate,
    memory_quality_attestation_dependencies,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity

REPORT_DIRECTORY_NAME = "reports"
REPORT_FILE_NAME = "curator-memory-quality.md"
ATTESTATION_FILE_NAME = "curator-memory-quality.json"
PROVENANCE_MISSING = "citation_provenance_missing"


@dataclass(frozen=True)
class CuratorChecklist:
    report_path: Path
    repo_id: str
    code_root: Path
    onboarding_root: Path
    pair_identity: MemoryCandidatePairIdentity
    code_candidate_tree: str
    memory_candidate_tree: str
    quality: dict[str, Any]
    repair_findings: list[dict[str, Any]]
    commit_owned_findings: list[dict[str, Any]]
    missing_onboarding: dict[str, Any]
    stale_route_indexes: list[str]
    source_candidates: tuple[CuratorSourceCandidate, ...]
    drift_rows: list[dict[str, Any]]
    report_only_findings: list[dict[str, Any]]


@dataclass(frozen=True)
class _ChecklistSections:
    status: str
    repair: list[dict[str, Any]]
    missing: list[dict[str, Any]]
    stale: list[str]
    source_candidates: list[dict[str, Any]]
    commit_owned: list[dict[str, Any]]
    report_only: list[dict[str, Any]]


def report_path_for(worktree_group: Path) -> Path:
    """Return the one curator checklist path reserved by a worktree enclosure."""
    return worktree_group / REPORT_DIRECTORY_NAME / REPORT_FILE_NAME


def split_commit_owned_findings(
    findings: list[dict[str, Any]], onboarding_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate repairable findings from new-card provenance only closeout can stamp.

    Temporary base provenance already clears dirty tracked cards during the curator pass.  A
    remaining missing-provenance row is closeout-owned only when its onboarding document is not
    tracked yet; treating every row with that code as closeout-owned would hide historical debt.
    """
    tracked = _tracked_onboarding_paths(onboarding_root)
    repairable: list[dict[str, Any]] = []
    commit_owned: list[dict[str, Any]] = []
    for finding in findings:
        path = str(finding.get("path", "")).replace("\\", "/").lstrip("/")
        if finding.get("code") == PROVENANCE_MISSING and path and path not in tracked:
            commit_owned.append(finding)
        else:
            repairable.append(finding)
    return repairable, commit_owned


def write_curator_checklist(checklist: CuratorChecklist) -> dict[str, Any]:
    """Atomically replace the deterministic checklist and return its compact wire summary."""
    missing = sorted(
        (dict(row) for row in checklist.missing_onboarding.get("missing", [])),
        key=lambda row: (
            str(row.get("sourceFile", "")),
            str(row.get("expectedOnboarding", "")),
        ),
    )
    stale = sorted(set(checklist.stale_route_indexes))
    repair = _sorted_findings(checklist.repair_findings)
    commit_owned = _sorted_findings(checklist.commit_owned_findings)
    report_only = _sorted_findings(checklist.report_only_findings)
    source_candidates = [
        {
            "source_file": row.sourceFile,
            "onboarding_file": row.onboardingFile,
            "classification": row.classification,
        }
        for row in checklist.source_candidates
    ]
    actionable_count = len(repair) + len(missing) + len(stale)
    status = "ready-for-closeout" if actionable_count == 0 else "action-required"
    sections = _ChecklistSections(
        status=status,
        repair=repair,
        missing=missing,
        stale=stale,
        source_candidates=source_candidates,
        commit_owned=commit_owned,
        report_only=report_only,
    )
    report = _render(checklist, sections)
    report_path = checklist.report_path.resolve()
    onboarding_root = checklist.onboarding_root.resolve()
    atomic_write_text(report_path, report)
    attestation_path = report_path.with_name(ATTESTATION_FILE_NAME)
    attestation = {
        "schema": "ar-curator-memory-quality/v1",
        "checklistStatus": status,
        "curatorActionableCount": actionable_count,
        "memoryRepairCount": len(repair),
        "missingOnboardingCount": len(missing),
        "staleRouteIndexCount": len(stale),
        "sourceChangeCandidateCount": len(source_candidates),
        "sourceChangeCandidates": [
            {
                "sourceFile": str(row.get("source_file", "")),
                "onboardingFile": str(row.get("onboarding_file", "")),
                "classification": str(row.get("classification", "")),
            }
            for row in source_candidates
        ],
        "pairIdentity": checklist.pair_identity.model_dump(mode="json"),
        "onboardingRoot": onboarding_root.as_posix(),
        "reportPath": report_path.as_posix(),
        "reportSha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
    }
    attestation["dependencies"] = memory_quality_attestation_dependencies(
        pair_identity=checklist.pair_identity,
        code_candidate_tree=checklist.code_candidate_tree,
        memory_candidate_tree=checklist.memory_candidate_tree,
        report_sha256=str(attestation["reportSha256"]),
    ).model_dump(mode="json")
    atomic_write_text(
        attestation_path,
        json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return {
        "reportPath": report_path.as_posix(),
        "attestationPath": attestation_path.as_posix(),
        "checklistStatus": status,
        "curatorActionableCount": actionable_count,
        "memoryRepairCount": len(repair),
        "missingOnboardingCount": len(missing),
        "staleRouteIndexCount": len(stale),
        "sourceChangeCandidateCount": len(source_candidates),
        "closeoutOwnedFindingCount": len(commit_owned),
        "noteworthyFindingCount": len(report_only),
    }


def _tracked_onboarding_paths(onboarding_root: Path) -> set[str]:
    memory_root = onboarding_root.parent
    result = run_git(memory_root, ["ls-files", "-z", "--", "onboarding"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git ls-files onboarding failed")
    prefix = "onboarding/"
    return {path[len(prefix) :] for path in result.stdout.split("\0") if path.startswith(prefix)}


def _sorted_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(finding) for finding in findings),
        key=lambda finding: (
            str(finding.get("check", "")),
            str(finding.get("path", "")),
            str(finding.get("code", "")),
            str(finding.get("message", "")),
        ),
    )


def _render(checklist: CuratorChecklist, sections: _ChecklistSections) -> str:
    lines = [
        "# Curator Memory Quality Checklist",
        "",
        f"- Status: **{sections.status}**",
        f"- Repository: `{_cell(checklist.repo_id)}`",
        f"- Code worktree: `{_cell(checklist.code_root.as_posix())}`",
        f"- Onboarding root: `{_cell(checklist.onboarding_root.as_posix())}`",
        f"- Pair contract: `{_cell(checklist.pair_identity.contractPath)}`",
        f"- Pair digest: `{checklist.pair_identity.contractDigest}`",
        "",
        (
            "This file is the current curator worklist. A full contract-scoped "
            "`memory_quality_check` atomically replaces it; it is not a durable task report "
            "and it is removed with the worktree enclosure."
        ),
        "",
        "## Summary",
        "",
        "| Class | Count | Gate meaning |",
        "| --- | ---: | --- |",
        f"| Repairable memory findings | {len(sections.repair)} | Must reach zero |",
        f"| Missing onboarding | {len(sections.missing)} | Must reach zero |",
        f"| Stale route indexes | {len(sections.stale)} | Must reach zero |",
        (
            f"| Source-change reconciliation candidates | {len(sections.source_candidates)} | "
            "Inspect and disposition; verification stamp clears at closeout |"
        ),
        (
            f"| Real-commit provenance findings | {len(sections.commit_owned)} | "
            "Closeout-owned; never fabricate a stamp |"
        ),
        f"| Noteworthy report-only findings | {len(sections.report_only)} | Review, not a gate |",
        (
            f"| Full quality finding count | "
            f"{int(checklist.quality.get('findingCount', 0))} | Context |"
        ),
        "",
    ]
    _append_findings(lines, "Repairable memory findings", sections.repair)
    _append_missing(lines, sections.missing)
    _append_paths(lines, "Stale route indexes", sections.stale)
    _append_drift(lines, sections.source_candidates)
    _append_drift(
        lines, checklist.drift_rows, heading="Full drift diagnostics (not candidate selection)"
    )
    _append_findings(lines, "Closeout-owned real-commit provenance", sections.commit_owned)
    _append_findings(lines, "Noteworthy report-only findings", sections.report_only)
    lines.extend(
        [
            "## Completion Rule",
            "",
            (
                "Rerun the same full contract-scoped `memory_quality_check` after repairs. "
                "The curator may hand off only when `curatorActionableCount` is zero and this "
                "report says `ready-for-closeout`; source-change candidates must also be "
                "explicitly dispositioned in the structured curator-coherence authority."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _append_findings(lines: list[str], heading: str, findings: list[dict[str, Any]]) -> None:
    lines.extend([f"## {heading}", ""])
    if not findings:
        lines.extend(["_None._", ""])
        return
    lines.extend(
        [
            "| Check | Path | Code | Message |",
            "| --- | --- | --- | --- |",
        ]
    )
    for finding in findings:
        lines.append(
            "| "
            + " | ".join(
                _cell(str(finding.get(key, ""))) for key in ("check", "path", "code", "message")
            )
            + " |"
        )
    lines.append("")


def _append_missing(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.extend(["## Missing onboarding", ""])
    if not rows:
        lines.extend(["_None._", ""])
        return
    lines.extend(
        [
            "| Source | Expected onboarding | State | Note |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _cell(str(row.get(key, "")))
                for key in ("sourceFile", "expectedOnboarding", "state", "note")
            )
            + " |"
        )
    lines.append("")


def _append_paths(lines: list[str], heading: str, paths: list[str]) -> None:
    lines.extend([f"## {heading}", ""])
    lines.extend([*(f"- `{_cell(path)}`" for path in paths), ""] if paths else ["_None._", ""])


def _append_drift(
    lines: list[str],
    rows: list[dict[str, Any]],
    *,
    heading: str = "Source-change reconciliation candidates",
) -> None:
    lines.extend([f"## {heading}", ""])
    if not rows:
        lines.extend(["_None._", ""])
        return
    lines.extend(
        [
            (
                "The governed candidate set is derived from exact structural census. Drift diagnostics "
                "remain context and do not expand that set. Rows do not count toward `curatorActionableCount`; a dirty "
                "worktree cannot truthfully receive its final commit stamp."
            ),
            "",
            "| Onboarding | Source | Classification | Note |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _cell(str(row.get(key, "")))
                for key in ("onboarding_file", "source_file", "classification", "note")
            )
            + " |"
        )
    lines.append("")


def _cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")
