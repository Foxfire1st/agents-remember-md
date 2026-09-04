"""CCR-R20 typed terminal rail-failure propagation at the detached worker boundary.

When an enforcing closeout rail fails and the outer lifecycle worker then crashes,
the authoritative terminal journal must preserve the exact failed rail IDs/versions,
stable failure codes, corrective owner classes, candidate/plan identities,
dependency-blocked or skipped rails, and bounded evidence references.  An
available typed rail result must never be replaced by a generic worker exception.

This module owns the bounded terminal rail-failure envelope and the census that
OperationRuntime.fail applies to outer Dagger/memory exception families:

- gate-result
    A current valid published rail report (schema-3.1 quality manifest whose
    candidate tree matches the accepted operation candidate) exists and its
    declared terminal artifact decodes.  The typed rail catalog is copied into
    the journal; failure facts never collapse to RuntimeError.
- terminal-rail-result-unavailable
    Rail evidence is present but missing, unreadable, or mismatched (candidate
    mismatch, unreadable manifest, missing/digest-invalid terminal artifact,
    malformed or contradictory decoded result).  No rail outcome is fabricated.
- worker-execution-unclassified
    No current valid rail report exists, so the worker died before rails
    published a result.  The census preserves the exact error type and any typed
    profile/executor family facts instead of collapsing them.

The envelope is the only thing this module writes: raw command output, secrets,
prompts, transcripts, and environment values are never copied into it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from agents_remember.certification.digests import content_digest
from agents_remember.certification.repository_profiles.adapters import JsonExitStatusDecoder
from agents_remember.errors import (
    CertificationContractError,
    CertificationExecutorPrerequisiteError,
    CertificationProfileError,
)
from agents_remember.worktrees.modules.quality.clean_executor import (
    published_generation_root,
    published_report_path_from_manifest,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    REPORT_SET_MANIFEST,
    PublishedQualityManifest,
    PublishedQualityManifestError,
    load_published_quality_manifest,
)

TERMINAL_RAIL_FAILURE_SCHEMA_VERSION: Final = "terminal-rail-failure-envelope/v1"

TerminalFailureClass = Literal[
    "gate-result",
    "terminal-rail-result-unavailable",
    "worker-execution-unclassified",
]

# Closed vocabulary shared with the CCR-R16 telemetry mirror.  These are the only
# terminal classes this module may publish; a new class must be added to both.
TERMINAL_RESULT_CLASSES: Final[tuple[TerminalFailureClass, ...]] = (
    "gate-result",
    "terminal-rail-result-unavailable",
    "worker-execution-unclassified",
)

_RAIL_STATUSES: Final[frozenset[str]] = frozenset(
    {"pass", "fail", "blocked", "skipped", "not-applicable"}
)
_POSTURES: Final[frozenset[str]] = frozenset({"enforcing", "report-only"})
_RAIL_EVIDENCE_KEYS: Final[frozenset[str]] = frozenset(
    {"evidenceId", "id", "sha256", "size", "reference", "ref"}
)
_RAIL_ARTIFACT_KEYS: Final[frozenset[str]] = frozenset(
    {"artifactId", "sha256", "size", "evidenceRef"}
)
# Keys that must never enter the public envelope, even when a rail row or report
# payload carries them: bounded public results exclude raw logs, secrets,
# environment values, prompts, transcripts, and command bytes.
_FORBIDDEN_PUBLIC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "stdout",
        "stderr",
        "log",
        "logs",
        "output",
        "outputs",
        "outputTail",
        "transcript",
        "prompt",
        "prompts",
        "secret",
        "secrets",
        "token",
        "tokens",
        "environment",
        "env",
        "command",
        "commands",
        "containerSocket",
        "credentials",
    }
)
_LOG_TAIL_MARKERS: Final[tuple[str, ...]] = (
    "\nQuality output tail:\n",
    "\nQuality output tail:",
    "\nQuality output:",
)

_BOUNDED_TEXT_MAX: Final = 8192
_DETAIL_MAX: Final = 4096
_ROW_DETAIL_MAX: Final = 2048
_FINDING_MAX: Final = 512
_MAX_CATALOG_ROWS: Final = 4096
_MAX_EVIDENCE_FILES: Final = 256

PublishedReadState = Literal[
    "published",
    "missing",
    "manifest-unreadable",
    "candidate-mismatch",
    "artifact-unreadable",
    "artifact-malformed",
]


@dataclass(frozen=True)
class PublishedReportRead:
    """Outcome of reading the current valid rail report for one operation."""

    state: PublishedReadState
    manifest: PublishedQualityManifest | None = None
    payload: dict[str, object] | None = None
    expected: dict[str, object] | None = None
    observed: dict[str, object] | None = None
    detail: str | None = None
    status: str | None = None
    exit_code: int | None = None


def terminal_worker_failure_result(
    *,
    operation_kind: str,
    generation: int,
    candidate_tree: str | None,
    error: BaseException | None = None,
    reports_dir: Path | None = None,
) -> dict[str, object]:
    """Terminalize one outer worker failure into the typed rail-failure envelope.

    The function is total: it always returns a bounded, JSON-serializable
    envelope and never raises, so the worker boundary can never fall back to an
    untyped generic wrapper when typed rail evidence exists.
    """

    report = _read_published_report(reports_dir, candidate_tree)
    census = None if error is None else _worker_error_census(error)
    if report.state == "published" and report.payload is not None:
        assert report.manifest is not None
        envelope = _gate_result_envelope(
            report,
            {
                "operationKind": operation_kind,
                "generation": generation,
                "candidateTree": candidate_tree,
            },
            census,
        )
    elif report.state in {
        "manifest-unreadable",
        "candidate-mismatch",
        "artifact-unreadable",
        "artifact-malformed",
    }:
        envelope = _unavailable_envelope(
            operation_kind=operation_kind,
            generation=generation,
            candidate_tree=candidate_tree,
            report=report,
            census=census,
        )
    else:
        envelope = _unclassified_envelope(
            operation_kind=operation_kind,
            generation=generation,
            candidate_tree=candidate_tree,
            census=census,
        )
    return _with_terminal_identity(envelope)


def _read_published_report(
    reports_dir: Path | None,
    candidate_tree: str | None,
) -> PublishedReportRead:
    """Strictly read and validate the one current published rail report.

    The schema-3.1 quality manifest self-validates its generation digest across
    candidate tree, profile/plan/selection/executor identities, decoder, files,
    dependencies, and runtime authority before any fact is copied.  A manifest
    whose candidate tree differs from the accepted operation candidate is a
    mismatch, never a usable report.
    """

    if reports_dir is None or not (reports_dir / REPORT_SET_MANIFEST).exists():
        return PublishedReportRead("missing")
    try:
        manifest = load_published_quality_manifest(reports_dir)
    except PublishedQualityManifestError as error:
        return PublishedReportRead(
            "manifest-unreadable",
            observed={"manifestError": _bounded_text(str(error), _DETAIL_MAX)},
            detail="published rail report manifest is missing, unreadable, or invalid",
        )
    if candidate_tree is not None and manifest.candidate_tree != candidate_tree:
        return PublishedReportRead(
            "candidate-mismatch",
            manifest=manifest,
            expected={"candidateTree": candidate_tree},
            observed={"candidateTree": manifest.candidate_tree},
            detail="published rail report targets a different candidate tree",
        )
    artifact = manifest.result_decoder.artifactPath
    try:
        published_report_path_from_manifest(reports_dir, manifest, artifact)
    except RuntimeError as error:
        return PublishedReportRead(
            "artifact-unreadable",
            manifest=manifest,
            observed={"manifestError": _bounded_text(str(error), _DETAIL_MAX)},
            detail="published rail report artifact is missing or failed generation verification",
        )
    root = published_generation_root(reports_dir, manifest)
    artifact_path = root / artifact
    decoded = _decode_terminal_payload(artifact_path, manifest)
    if decoded is None:
        return PublishedReportRead(
            "artifact-malformed",
            manifest=manifest,
            observed={"artifactPath": artifact, "artifactState": "undecodable"},
            detail="published rail report terminal artifact is malformed or contradictory",
        )
    payload, status, exit_code = decoded
    return PublishedReportRead(
        "published",
        manifest=manifest,
        payload=payload,
        status=status,
        exit_code=exit_code,
    )


def _decode_terminal_payload(
    artifact_path: Path,
    manifest: PublishedQualityManifest,
) -> tuple[dict[str, object], str, int] | None:
    """Decode the declared terminal artifact exactly as the executor decoder would.

    The R22 executor decoder is the sole validation authority: it parses the
    JSON, refuses a non-object root, verifies every declared artifact reference,
    and rejects invalid or contradictory status/exit values.  This reader only
    re-reads those verified bytes to copy the payload the decoder accepted, so
    there is no second, weaker validation path to drift from it.
    """

    try:
        decoded = JsonExitStatusDecoder().decode(
            manifest.result_decoder,
            artifact_path.parent,
            manifest.files,
        )
    except RuntimeError:
        return None
    parsed = json.loads(artifact_path.read_text(encoding="utf-8"))
    return cast(dict[str, object], parsed), decoded.status, decoded.exit_code


def _gate_result_envelope(
    report: PublishedReportRead,
    operation: Mapping[str, object],
    census: dict[str, object] | None,
) -> dict[str, object]:
    """Copy the validated typed rail catalog into one bounded gate-result envelope."""

    manifest = report.manifest
    payload = report.payload
    assert manifest is not None and payload is not None
    operation_kind = str(operation["operationKind"])
    raw_generation = operation.get("generation")
    generation = int(raw_generation) if isinstance(raw_generation, int) else 1
    raw_candidate = operation.get("candidateTree")
    candidate_tree = raw_candidate if isinstance(raw_candidate, str) else None
    rows = _catalog_rows(payload)
    failed, blocked, skipped = _split_terminal_rows(rows)
    disposition, counts = _rail_disposition(rows)
    manifest_identity = _manifest_identity(manifest)
    evidence = _bounded_evidence_records(manifest)
    exit_code = report.exit_code if isinstance(report.exit_code, int) else 1
    status = report.status if isinstance(report.status, str) else ""
    reason = _published_reason(
        operation_kind,
        manifest.profile_plan_digest,
        payload,
        disposition,
        exit_code,
    )
    envelope: dict[str, object] = {
        "schemaVersion": TERMINAL_RAIL_FAILURE_SCHEMA_VERSION,
        "terminalClass": "gate-result",
        "operationKind": operation_kind,
        "generation": generation,
        "candidateTree": candidate_tree,
        "reason": reason,
        "error": census,
        "report": {
            **manifest_identity,
            "candidateTree": manifest.candidate_tree,
            "generation": manifest.generation,
        },
        "terminalResult": _terminal_result_facts(
            payload,
            disposition,
            status=status,
            exit_code=exit_code,
        ),
        "rails": {
            "failed": failed,
            "blocked": blocked,
            "skipped": skipped,
        },
        "counts": counts,
        "gateResultManifestId": _catalog_manifest_identity(rows, counts),
        "evidence": evidence,
    }
    return _drop_absent(envelope)


def _unavailable_envelope(
    *,
    operation_kind: str,
    generation: int,
    candidate_tree: str | None,
    report: PublishedReportRead,
    census: dict[str, object] | None,
) -> dict[str, object]:
    """Typed unavailable envelope: evidence exists but cannot be validated."""

    observed: dict[str, object] = dict(report.observed or {})
    if report.manifest is not None:
        observed["manifestIdentity"] = _manifest_identity(report.manifest)
    envelope: dict[str, object] = {
        "schemaVersion": TERMINAL_RAIL_FAILURE_SCHEMA_VERSION,
        "terminalClass": "terminal-rail-result-unavailable",
        "operationKind": operation_kind,
        "generation": generation,
        "candidateTree": candidate_tree,
        "reason": report.detail or "terminal rail evidence is unavailable or mismatched",
        "error": census,
        "expected": report.expected,
        "observed": observed,
    }
    return _drop_absent(envelope)


def _unclassified_envelope(
    *,
    operation_kind: str,
    generation: int,
    candidate_tree: str | None,
    census: dict[str, object] | None,
) -> dict[str, object]:
    """Pre-rail worker crash envelope; never used when a valid rail report exists."""

    expected: dict[str, object] = {"publishedRailReport": "current valid report"}
    if candidate_tree is not None:
        expected["candidateTree"] = candidate_tree
    envelope: dict[str, object] = {
        "schemaVersion": TERMINAL_RAIL_FAILURE_SCHEMA_VERSION,
        "terminalClass": "worker-execution-unclassified",
        "operationKind": operation_kind,
        "generation": generation,
        "candidateTree": candidate_tree,
        "reason": (
            "operation worker failed before publishing a typed rail result; no current "
            "valid rail report exists for this candidate"
        ),
        "error": census,
        "expected": expected,
        "observed": {"publishedRailReport": None},
    }
    return _drop_absent(envelope)


def _catalog_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    """Ordered canonical rail rows copied from the published gate catalogs.

    Only the closed canonical vocabulary is copied; unknown or forbidden keys
    (raw output, secrets, prompts) never enter the journal projection.
    """

    gates = payload.get("gates")
    if not isinstance(gates, list):
        return []
    rows: list[dict[str, object]] = []
    for gate_value in gates:
        if not isinstance(gate_value, dict):
            continue
        gate_rows = gate_value.get("rails")
        if not isinstance(gate_rows, list):
            continue
        for row in gate_rows:
            canonical = _canonical_rail_row(row)
            if canonical is not None:
                rows.append(canonical)
    return rows[:_MAX_CATALOG_ROWS]


def _canonical_rail_row(row: object) -> dict[str, object] | None:
    """Project one typed rail catalog row onto the bounded envelope vocabulary."""

    if not isinstance(row, dict):
        return None
    rail_id, version, key = _rail_identity(row)
    if rail_id is None and key is None:
        return None
    status = row.get("status")
    if status not in _RAIL_STATUSES:
        return None
    canonical: dict[str, object] = {
        "railId": rail_id,
        "version": version,
        "key": key,
        "status": status,
    }
    canonical.update(_row_posture_gate(row))
    canonical.update(_canonical_code_owner(row))
    blockers = _canonical_blocked_by(row)
    if blockers is not None:
        canonical["blockedBy"] = blockers
    canonical.update(_row_exit_and_detail(row))
    evidence = _canonical_bounded_list(row.get("evidence"), _RAIL_EVIDENCE_KEYS)
    if evidence:
        canonical["evidence"] = evidence
    artifacts = _canonical_bounded_list(row.get("artifacts"), _RAIL_ARTIFACT_KEYS)
    if artifacts:
        canonical["artifacts"] = artifacts
    digest = row.get("resultDigest")
    if isinstance(digest, str) and len(digest) == 64:
        canonical["resultDigest"] = digest
    return canonical


def _row_posture_gate(row: Mapping[str, object]) -> dict[str, object]:
    """Copy the enforcing posture and gate identity when the row declares them."""

    fields: dict[str, object] = {}
    posture = row.get("posture")
    if posture in _POSTURES:
        fields["posture"] = posture
    gate = row.get("gate")
    if isinstance(gate, int) and not isinstance(gate, bool):
        fields["gate"] = gate
    return fields


def _row_exit_and_detail(row: Mapping[str, object]) -> dict[str, object]:
    """Bounded exit code and rail detail; never a raw output or log field."""

    fields: dict[str, object] = {}
    exit_code = row.get("exitCode")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        fields["exitCode"] = exit_code
    detail = row.get("detail")
    if isinstance(detail, str):
        bounded = _bounded_text(detail, _ROW_DETAIL_MAX)
        if bounded is not None:
            fields["detail"] = bounded
    return fields


def _rail_identity(row: Mapping[str, object]) -> tuple[str | None, str | None, str | None]:
    """Resolve rail id/version/key from an R11 row or a Dagger catalog row."""

    key = _nonblank_text(row.get("key"))
    identity_mapping = _identity_mapping(row)
    rail_id = _first_rail_id(row) or _first_rail_id(identity_mapping)
    version = _first_version(row, identity_mapping)
    rail_id, version = _key_identity(key, rail_id, version)
    return rail_id, version, _derived_key(key, rail_id, version)


def _nonblank_text(value: object) -> str | None:
    """One nonblank text value, or None when absent or empty."""

    if isinstance(value, str) and value:
        return value
    return None


def _identity_mapping(row: Mapping[str, object]) -> Mapping[str, object]:
    """The declared identity mapping of an R11 row or a Dagger catalog row."""

    identity = row.get("identity") or row.get("rail")
    return identity if isinstance(identity, dict) else {}


def _key_identity(
    key: str | None,
    rail_id: str | None,
    version: str | None,
) -> tuple[str | None, str | None]:
    """Fill a missing rail id or version from a railId@version key alone."""

    if rail_id is not None or key is None or "@" not in key:
        return rail_id, version
    left, _, right = key.partition("@")
    resolved_id = left or None
    resolved_version = version
    if resolved_version is None and right:
        resolved_version = right
    return resolved_id, resolved_version


def _derived_key(
    key: str | None,
    rail_id: str | None,
    version: str | None,
) -> str | None:
    """One canonical rail key derived only when identity and version both exist."""

    if key is None and rail_id is not None and version is not None:
        return f"{rail_id}@{version}"
    return key


def _first_rail_id(mapping: Mapping[str, object]) -> str | None:
    """The first nonblank rail identity declared by a row or identity mapping."""

    for id_key in ("railId", "id"):
        value = mapping.get(id_key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_version(*mappings: Mapping[str, object]) -> str | None:
    """The first nonblank semantic version declared by any supplied mapping."""

    for mapping in mappings:
        value = mapping.get("version")
        if isinstance(value, str) and value:
            return value
    return None


def _canonical_code_owner(row: dict[str, object]) -> dict[str, object]:
    """Stable failure code and corrective owner class when the row declares them."""

    canonical: dict[str, object] = {}
    code = row.get("code") or row.get("failureCode")
    if isinstance(code, str) and code:
        canonical["code"] = _bounded_text(code, _FINDING_MAX) or code[:_FINDING_MAX]
    owner = row.get("owner") or row.get("correctiveOwner") or row.get("ownerClass")
    if isinstance(owner, str) and owner:
        canonical["owner"] = _bounded_text(owner, _FINDING_MAX) or owner[:_FINDING_MAX]
    return canonical


def _canonical_blocked_by(row: dict[str, object]) -> list[str] | None:
    """blockedBy rail identities or selector ids as an ordered bounded list."""

    value = row.get("blockedBy")
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    blockers = [label for item in value if (label := _blocked_label(item)) is not None]
    return blockers[:_MAX_CATALOG_ROWS] or None


def _blocked_label(item: object) -> str | None:
    """One canonical blocker label from a string or an R11 identity record."""

    if isinstance(item, str):
        return item if item else None
    if not isinstance(item, dict):
        return None
    item_id, _version, item_key = _rail_identity(item)
    if item_key is not None:
        return item_key
    if item_id is not None:
        return item_id
    return None


def _canonical_bounded_list(
    value: object,
    allowed_keys: frozenset[str],
) -> list[dict[str, object]]:
    """Copy bounded JSON records keeping only the closed evidence vocabulary."""

    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        record = _bounded_record(item, allowed_keys)
        if record is not None:
            records.append(record)
    return records[:_MAX_CATALOG_ROWS]


def _bounded_record(
    item: object,
    allowed_keys: frozenset[str],
) -> dict[str, object] | None:
    """One bounded evidence record; raw or unknown values never pass through."""

    if not isinstance(item, dict):
        return None
    bounded: dict[str, object] = {}
    for key, member in item.items():
        if key not in allowed_keys:
            continue
        bounded_value = _bounded_member(member)
        if bounded_value is None:
            continue
        bounded[key] = bounded_value
    return bounded or None


def _bounded_member(member: object) -> object | None:
    """One bounded member value: text is capped; null values are dropped."""

    if member is None:
        return None
    if isinstance(member, str):
        return _bounded_text(member, _FINDING_MAX)
    return member


def _split_terminal_rows(
    rows: Sequence[dict[str, object]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Separate failed, blocked, and skipped rails; never truncate siblings."""

    failed = [row for row in rows if row["status"] == "fail"]
    blocked = [row for row in rows if row["status"] == "blocked"]
    skipped = [row for row in rows if row["status"] == "skipped"]
    return failed, blocked, skipped


def _rail_disposition(
    rows: Sequence[dict[str, object]],
) -> tuple[Literal["green", "red"], dict[str, int]]:
    """Gate-level disposition and complete status counts from the ordered catalog."""

    counts = {"passed": 0, "failed": 0, "blocked": 0, "skipped": 0, "notApplicable": 0}
    red = False
    for row in rows:
        status = row["status"]
        posture = row.get("posture")
        if status == "pass":
            counts["passed"] += 1
        elif status == "fail":
            counts["failed"] += 1
            if posture != "report-only":
                red = True
        elif status == "blocked":
            counts["blocked"] += 1
            if posture != "report-only":
                red = True
        elif status == "skipped":
            counts["skipped"] += 1
        else:
            counts["notApplicable"] += 1
    return ("red" if red else "green", counts)


def _manifest_identity(manifest: PublishedQualityManifest) -> dict[str, object]:
    """The exact plan/executor/runtime identity bound by the published manifest."""

    return {
        "profileDigest": manifest.profile_digest,
        "profilePlanDigest": manifest.profile_plan_digest,
        "profileSelectionId": manifest.profile_selection_id,
        "executorAdapterId": manifest.executor_adapter_id,
        "resultDecoder": manifest.result_decoder.model_dump(mode="json"),
        "runtimeAuthorityDigest": manifest.runtime_authority_digest,
    }


def _terminal_result_facts(
    payload: dict[str, object],
    disposition: Literal["green", "red"],
    *,
    status: str,
    exit_code: int,
) -> dict[str, object]:
    """Bounded top-level terminal facts from the decoded artifact.

    status and exit_code were already validated by the executor decoder, so the
    facts never re-derive them from the raw payload.
    """

    facts: dict[str, object] = {
        "disposition": disposition,
        "status": _bounded_text(status, _FINDING_MAX) or status[:_FINDING_MAX],
        "exitCode": exit_code,
    }
    failed_step = payload.get("failedStep")
    if isinstance(failed_step, str):
        bounded_step = _bounded_text(failed_step, _FINDING_MAX)
        facts["failedStep"] = (
            bounded_step if bounded_step is not None else failed_step[:_FINDING_MAX]
        )
    return facts


def _catalog_manifest_identity(
    rows: Sequence[dict[str, object]],
    counts: dict[str, int],
) -> str:
    """One deterministic manifest identity over the ordered rail terminal set."""

    rail_results: list[dict[str, object]] = []
    for row in rows:
        row_digest = content_digest(row)
        rail_results.append(
            {
                "rail": row.get("key") or row.get("railId"),
                "resultId": row_digest,
                "status": row["status"],
                "posture": row.get("posture") or "enforcing",
            }
        )
    return content_digest({"railResults": rail_results, "counts": counts})


def _bounded_evidence_records(manifest: PublishedQualityManifest) -> list[dict[str, object]]:
    """Bounded digest/path references to the published canonical evidence files."""

    records: list[dict[str, object]] = []
    for name in sorted(manifest.files)[:_MAX_EVIDENCE_FILES]:
        file_record = manifest.files[name]
        records.append(
            {
                "path": name,
                "sha256": file_record.sha256,
                "size": file_record.size,
            }
        )
    return records


def _published_reason(
    operation_kind: str,
    profile_plan_digest: str,
    payload: dict[str, object],
    disposition: Literal["green", "red"],
    exit_code: int,
) -> str:
    """A bounded reason never carrying raw command output or log tails."""

    if disposition == "red":
        detail = _published_failure_detail(payload, exit_code)
        return (
            f"{operation_kind} operation terminalized with a red rail report "
            f"(profilePlanDigest={profile_plan_digest}){detail}"
        )
    return (
        f"{operation_kind} operation failed after its rail report passed "
        f"(profilePlanDigest={profile_plan_digest})"
    )


def _published_failure_detail(payload: dict[str, object], exit_code: int) -> str:
    """One bounded, log-free failure detail drawn from the typed decoded result."""

    parts: list[str] = []
    failed_step = payload.get("failedStep")
    if isinstance(failed_step, str):
        bounded_step = _bounded_text(failed_step, _FINDING_MAX)
        parts.append(f" failedStep={bounded_step}")
    parts.append(f" exitCode={exit_code}")
    return "".join(parts)


def _worker_error_census(error: BaseException) -> dict[str, object]:
    """One bounded census of the outer exception family.

    Profile, executor-prerequisite, report-publication, and every other typed
    family keeps its stable status code and bounded findings; only an error with
    no typed identity is reported under its exception type.
    """

    detail = _bounded_failure_text(error)
    if isinstance(error, CertificationProfileError):
        return {
            "type": "CertificationProfileError",
            "status": error.status,
            "stage": "profile-admission",
            "findings": _bounded_findings(error),
            "detail": detail,
        }
    if isinstance(error, CertificationExecutorPrerequisiteError):
        return {
            "type": "CertificationExecutorPrerequisiteError",
            "status": error.status,
            "stage": "executor-prerequisite",
            "findings": _bounded_findings(error),
            "detail": detail,
        }
    if isinstance(error, PublishedQualityManifestError):
        return {
            "type": "PublishedQualityManifestError",
            "status": "published-quality-manifest-invalid",
            "stage": "report-publication",
            "detail": detail,
        }
    if isinstance(error, CertificationContractError):
        status = getattr(error, "status", None)
        return {
            "type": type(error).__name__,
            "status": status,
            "stage": "certification-contract",
            "findings": _bounded_findings(error),
            "detail": detail,
        }
    return {
        "type": type(error).__name__,
        "status": None,
        "stage": "worker-execution",
        "detail": detail,
    }


def _bounded_findings(error: CertificationContractError) -> list[dict[str, object]]:
    """Complete typed finding sequence, never first-failure-only."""

    findings: list[dict[str, object]] = []
    for finding in error.findings:
        bounded: dict[str, object] = {}
        for key in ("code", "path", "detail"):
            value = finding.get(key)
            if isinstance(value, str):
                value = _bounded_text(value, _FINDING_MAX) or value[:_FINDING_MAX]
            if value is not None and value != "":
                bounded[key] = value
        if bounded:
            findings.append(bounded)
    return findings[:_MAX_CATALOG_ROWS]


def _bounded_failure_text(error: BaseException) -> str | None:
    """The exception message stripped of any embedded raw command/log tail."""

    text = str(error)
    for marker in _LOG_TAIL_MARKERS:
        if marker in text:
            text = text.split(marker, 1)[0].rstrip()
            break
    return _bounded_text(text, _DETAIL_MAX)


def _bounded_text(value: str, max_length: int) -> str | None:
    """Trim one text value to a bounded public length without splitting escapes."""

    if not value:
        return None
    if len(value) <= max_length:
        return value
    return f"{value[:max_length]}\u2026"


def _drop_absent(value: dict[str, object]) -> dict[str, object]:
    """Remove optional members whose value is None so the envelope stays minimal."""

    return {key: member for key, member in value.items() if member is not None}


def _with_terminal_identity(envelope: dict[str, object]) -> dict[str, object]:
    """Stamp the terminalId over the complete bounded envelope content.

    The identity digest covers every other envelope member, so journal, status,
    wait, and telemetry mirrors all expose the same result identity for the same
    rail facts.
    """

    identity = content_digest(
        {key: value for key, value in envelope.items() if key != "terminalId"}
    )
    return {**envelope, "terminalId": identity}


def telemetry_terminal_facts(
    envelope: Mapping[str, object],
) -> dict[str, object]:
    """The CCR-R16 terminal-event view of one terminal envelope.

    Telemetry adds timing; it never carries rail facts the journal lacks.  The
    view returns exactly the terminal identity and closed class the R16
    operation-terminal payload mirrors.
    """

    terminal_class = envelope.get("terminalClass")
    if terminal_class not in TERMINAL_RESULT_CLASSES:
        raise ValueError(f"terminal class {terminal_class!r} is not in the closed vocabulary")
    terminal_id = envelope.get("terminalId")
    if not isinstance(terminal_id, str) or len(terminal_id) != 64:
        raise ValueError("terminal envelope requires a content-bound terminalId")
    facts: dict[str, object] = {
        "terminalId": terminal_id,
        "terminalResultClass": terminal_class,
    }
    if terminal_class == "gate-result":
        manifest_id = envelope.get("gateResultManifestId")
        if isinstance(manifest_id, str):
            facts["gateResultManifestId"] = manifest_id
    return facts


def worker_failure_result_class(result: Mapping[str, object] | None) -> str | None:
    """The typed terminal class of one journal result, if it is an envelope."""

    if not isinstance(result, Mapping):
        return None
    if result.get("schemaVersion") != TERMINAL_RAIL_FAILURE_SCHEMA_VERSION:
        return None
    terminal_class = result.get("terminalClass")
    if terminal_class in TERMINAL_RESULT_CLASSES:
        return terminal_class
    return None


__all__ = [
    "TERMINAL_RAIL_FAILURE_SCHEMA_VERSION",
    "TERMINAL_RESULT_CLASSES",
    "PublishedReportRead",
    "telemetry_terminal_facts",
    "terminal_worker_failure_result",
    "worker_failure_result_class",
]
