"""Explicit audited adoption of pre-locator readable worktree enclosures."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError

from agents_remember.kernel.atomic_write import atomic_write_bytes, atomic_write_text
from agents_remember.models.base import StrictResponseModel
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    EnclosurePublicationArtifacts,
    LifecycleOperationLocation,
    LifecycleOperationLocationError,
    prepare_enclosure_publication,
    publish_enclosure_location,
)
from agents_remember.worktrees.worktree_contract import load_contract

ADOPTION_RECEIPT = "enclosure-adoption-receipt.json"
_LEGACY_ARTIFACT = re.compile(
    r"^(?:closeout|integrate|direct-landing)-operation"
    r"(?:\.generation-[1-9][0-9]*)?\.(?:json|log)$"
)
_LEGACY_MISSING_INTENT_ARTIFACT = re.compile(
    r"^(?:closeout|direct-landing)-operation"
    r"\.legacy-missing-intent-generation-[1-9][0-9]*\.json$"
)


class AdoptedLifecycleArtifact(StrictResponseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class LifecycleEnclosureAdoptionReceipt(StrictResponseModel):
    """Idempotent audit proof for one explicit old-layout adoption request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schemaVersion: Literal["1.0"] = "1.0"
    publicationRequestId: str = Field(pattern=r"^[0-9a-f]{64}$")
    enclosurePublicationRequestId: str = Field(pattern=r"^[0-9a-f]{64}$")
    contractPath: str = Field(min_length=1, max_length=4096)
    contractSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    worktreeGroup: str = Field(min_length=1, max_length=4096)
    manifestSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    auditIntent: str = Field(min_length=1, max_length=4096)
    artifacts: list[AdoptedLifecycleArtifact]


@dataclass(frozen=True)
class LifecycleEnclosureAdoptionPreview:
    artifacts: EnclosurePublicationArtifacts
    receipt: LifecycleEnclosureAdoptionReceipt
    source_paths: tuple[Path, ...]

    def payload(self) -> dict[str, object]:
        return {
            "state": "would-adopt-enclosure",
            "status": "would-adopt-enclosure",
            "contractPath": self.artifacts.contract_path.as_posix(),
            "expectedWorktreeGroup": self.artifacts.reserved_locator.worktreeGroup,
            "locatorPath": self.artifacts.locator_path.as_posix(),
            "manifestPath": self.artifacts.manifest_path.as_posix(),
            "publicationRequestId": self.receipt.publicationRequestId,
            "contractSha256": self.receipt.contractSha256,
            "manifestSha256": self.receipt.manifestSha256,
            "artifacts": [item.model_dump(mode="json") for item in self.receipt.artifacts],
            "sourcePaths": [path.as_posix() for path in self.source_paths],
            "removalCondition": (
                "remove worktree_enclosure_adopt only after the explicit inventory contains "
                "no readable enclosure without an addressable locator and root manifest"
            ),
        }


def preview_lifecycle_enclosure_adoption(
    contract_path: Path,
    *,
    expected_worktree_group: Path,
    rationale: str,
) -> LifecycleEnclosureAdoptionPreview:
    """Read one explicit old enclosure without publishing or inferring a binding."""

    intent = rationale.strip()
    if not intent:
        raise ValueError("enclosure adoption requires a nonblank rationale")
    contract_bytes = _read_bytes(contract_path, "contract")
    try:
        contract = load_contract(contract_path)
    except Exception as exc:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-invalid",
            "explicit enclosure adoption requires a readable current worktree contract",
            expected={"contractPath": contract_path.resolve(strict=False).as_posix()},
            observed={"errorType": type(exc).__name__},
        ) from exc
    expected_root = expected_worktree_group.resolve(strict=False)
    observed_root = contract.worktree_group.resolve(strict=False)
    if observed_root != expected_root:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-conflict",
            "the explicit expected enclosure root contradicts the readable contract",
            expected={"worktreeGroup": expected_root.as_posix()},
            observed={"worktreeGroup": observed_root.as_posix()},
        )
    contract_text = contract_bytes.decode("utf-8")
    artifacts = prepare_enclosure_publication(
        contract,
        contract_text=contract_text,
        publication_kind="legacy-adoption",
        audit_intent=intent,
    )
    receipt_path = artifacts.manifest_path.parent / ADOPTION_RECEIPT
    existing_receipt = _read_receipt_if_present(receipt_path, contract_path)
    if existing_receipt is not None:
        receipt = _require_matching_receipt(existing_receipt, artifacts, intent)
        source_paths = tuple(
            contract.worktree_group / "reports" / item.name for item in receipt.artifacts
        )
        _require_receipt_artifacts(receipt, artifacts, source_paths)
        return LifecycleEnclosureAdoptionPreview(artifacts, receipt, source_paths)
    source_paths = _legacy_artifact_paths(contract.worktree_group / "reports", contract_path)
    target_paths = _root_artifact_paths(artifacts.manifest_path.parent, contract_path)
    if target_paths and not source_paths:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-conflict",
            "root-local operation bytes exist without an adoption receipt or legacy originals",
            expected={"legacyReports": "exact source bytes or an exact adoption receipt"},
            observed={"rootArtifacts": [path.name for path in target_paths]},
        )
    _require_preview_target_compatibility(source_paths, target_paths)
    adopted = [_artifact(path) for path in source_paths]
    receipt = LifecycleEnclosureAdoptionReceipt(
        publicationRequestId=_adoption_request_id(artifacts, intent, adopted),
        enclosurePublicationRequestId=artifacts.reserved_locator.publicationRequestId,
        contractPath=artifacts.contract_path.as_posix(),
        contractSha256=_sha256(contract_bytes),
        worktreeGroup=expected_root.as_posix(),
        manifestSha256=artifacts.reserved_locator.expectedManifestSha256,
        auditIntent=intent,
        artifacts=adopted,
    )
    return LifecycleEnclosureAdoptionPreview(artifacts, receipt, source_paths)


def apply_lifecycle_enclosure_adoption(
    preview: LifecycleEnclosureAdoptionPreview,
) -> tuple[LifecycleOperationLocation, LifecycleEnclosureAdoptionReceipt]:
    """Publish the exact preview under locator authority and retire old active copies."""

    def copy_legacy_artifacts() -> None:
        current = preview_lifecycle_enclosure_adoption(
            preview.artifacts.contract_path,
            expected_worktree_group=Path(preview.receipt.worktreeGroup),
            rationale=preview.receipt.auditIntent,
        )
        if current.receipt != preview.receipt:
            raise LifecycleOperationLocationError(
                "operation-location-adoption-conflict",
                "the explicit adoption evidence changed between preview and apply",
                expected=preview.receipt.model_dump(mode="json"),
                observed=current.receipt.model_dump(mode="json"),
            )
        for item, source in zip(preview.receipt.artifacts, current.source_paths, strict=True):
            target = preview.artifacts.manifest_path.parent / item.name
            source_bytes = _read_bytes(source, "legacy operation")
            _require_artifact_bytes(item, source_bytes, side="legacy")
            if target.exists():
                target_bytes = _read_bytes(target, "root operation")
                _require_artifact_bytes(item, target_bytes, side="root")
            else:
                _publish_adoption_bytes(target, source_bytes, item)
                _require_artifact_bytes(
                    item,
                    _read_bytes(target, "root operation"),
                    side="root",
                )

    def publish_receipt(location: LifecycleOperationLocation) -> None:
        receipt_path = location.lifecycle_directory / ADOPTION_RECEIPT
        receipt_text = preview.receipt.model_dump_json(indent=2) + "\n"
        if receipt_path.exists():
            observed = _read_bytes(receipt_path, "adoption receipt")
            if observed != receipt_text.encode("utf-8"):
                raise LifecycleOperationLocationError(
                    "operation-location-adoption-conflict",
                    "the enclosure adoption receipt contains different request evidence",
                    expected={"sha256": _sha256(receipt_text.encode())},
                    observed={"sha256": _sha256(observed)},
                )
        else:
            _publish_adoption_receipt(receipt_path, receipt_text)
        for item, source in zip(preview.receipt.artifacts, preview.source_paths, strict=True):
            if not source.exists():
                continue
            source_bytes = _read_bytes(source, "legacy operation")
            _require_artifact_bytes(item, source_bytes, side="legacy")
            _retire_legacy_source(source, item)

    location = publish_enclosure_location(
        preview.artifacts,
        contract_mode="prove-existing",
        before_contract_proof=copy_legacy_artifacts,
        after_addressable=publish_receipt,
    )
    return location, preview.receipt


def _legacy_artifact_paths(reports: Path, contract_path: Path) -> tuple[Path, ...]:
    if not reports.exists():
        return ()
    try:
        return tuple(
            sorted(
                (
                    path
                    for path in reports.iterdir()
                    if path.is_file() and _is_legacy_artifact(path.name)
                ),
                key=lambda path: path.name,
            )
        )
    except OSError as exc:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-invalid",
            "the explicitly targeted legacy reports directory is unreadable",
            expected={"contractPath": contract_path.as_posix()},
            observed={"reportsPath": reports.as_posix(), "errorType": type(exc).__name__},
        ) from exc


def _root_artifact_paths(lifecycle: Path, contract_path: Path) -> tuple[Path, ...]:
    if not lifecycle.exists():
        return ()
    try:
        return tuple(
            sorted(
                (
                    path
                    for path in lifecycle.iterdir()
                    if path.is_file() and _is_legacy_artifact(path.name)
                ),
                key=lambda path: path.name,
            )
        )
    except OSError as exc:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-invalid",
            "the explicitly targeted lifecycle directory is unreadable",
            expected={"contractPath": contract_path.as_posix()},
            observed={"lifecyclePath": lifecycle.as_posix(), "errorType": type(exc).__name__},
        ) from exc


def _is_legacy_artifact(name: str) -> bool:
    return (
        _LEGACY_ARTIFACT.fullmatch(name) is not None
        or _LEGACY_MISSING_INTENT_ARTIFACT.fullmatch(name) is not None
    )


def _require_preview_target_compatibility(
    source_paths: tuple[Path, ...],
    target_paths: tuple[Path, ...],
) -> None:
    sources = {path.name: _artifact(path) for path in source_paths}
    targets = {path.name: path for path in target_paths}
    if set(targets) - set(sources):
        raise LifecycleOperationLocationError(
            "operation-location-adoption-conflict",
            "the root contains an operation artifact absent from the explicit legacy source",
            expected={"artifactNames": sorted(sources)},
            observed={"artifactNames": sorted(targets)},
        )
    for name, target in targets.items():
        _require_artifact_bytes(
            sources[name],
            _read_bytes(target, "root operation"),
            side="root",
        )


def _read_receipt_if_present(
    path: Path,
    contract_path: Path,
) -> LifecycleEnclosureAdoptionReceipt | None:
    if not path.exists():
        return None
    try:
        return LifecycleEnclosureAdoptionReceipt.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-conflict",
            "the enclosure adoption receipt is unreadable or invalid",
            expected={"contractPath": contract_path.as_posix()},
            observed={"receiptPath": path.as_posix(), "errorType": type(exc).__name__},
        ) from exc


def _require_matching_receipt(
    receipt: LifecycleEnclosureAdoptionReceipt,
    artifacts: EnclosurePublicationArtifacts,
    intent: str,
) -> LifecycleEnclosureAdoptionReceipt:
    expected = {
        "publicationRequestId": _adoption_request_id(
            artifacts,
            intent,
            receipt.artifacts,
        ),
        "enclosurePublicationRequestId": artifacts.reserved_locator.publicationRequestId,
        "contractPath": artifacts.contract_path.as_posix(),
        "contractSha256": artifacts.reserved_locator.expectedInitialContractSha256,
        "worktreeGroup": artifacts.reserved_locator.worktreeGroup,
        "manifestSha256": artifacts.reserved_locator.expectedManifestSha256,
        "auditIntent": intent,
    }
    observed = receipt.model_dump(
        mode="json",
        exclude={"artifacts", "schemaVersion"},
    )
    if observed != expected:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-conflict",
            "the existing adoption receipt belongs to a different request",
            expected=expected,
            observed=observed,
        )
    return receipt


def _require_receipt_artifacts(
    receipt: LifecycleEnclosureAdoptionReceipt,
    artifacts: EnclosurePublicationArtifacts,
    source_paths: tuple[Path, ...],
) -> None:
    for item, source in zip(receipt.artifacts, source_paths, strict=True):
        target = artifacts.manifest_path.parent / item.name
        if not target.is_file():
            raise LifecycleOperationLocationError(
                "operation-location-adoption-conflict",
                "the receipt names a missing canonical root operation artifact",
                expected=item.model_dump(mode="json"),
                observed={"path": target.as_posix(), "state": "missing"},
            )
        _require_artifact_bytes(item, _read_bytes(target, "root operation"), side="root")
        if source.exists():
            _require_artifact_bytes(item, _read_bytes(source, "legacy operation"), side="legacy")


def _artifact(path: Path) -> AdoptedLifecycleArtifact:
    payload = _read_bytes(path, "legacy operation")
    return AdoptedLifecycleArtifact(name=path.name, sha256=_sha256(payload), size=len(payload))


def _require_artifact_bytes(
    item: AdoptedLifecycleArtifact,
    payload: bytes,
    *,
    side: str,
) -> None:
    observed = {"name": item.name, "sha256": _sha256(payload), "size": len(payload)}
    expected = item.model_dump(mode="json")
    if observed != expected:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-conflict",
            "an adoption operation artifact changed after preview",
            expected=expected,
            observed={"side": side, **observed},
        )


def _retire_legacy_source(source: Path, item: AdoptedLifecycleArtifact) -> None:
    """Remove only an exact copied source, classifying interrupted retirement."""

    try:
        source.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        try:
            observed = _read_bytes(source, "legacy operation")
        except LifecycleOperationLocationError:
            observed_payload: dict[str, object] = {
                "path": source.as_posix(),
                "errorType": type(exc).__name__,
            }
        else:
            observed_payload = {
                "path": source.as_posix(),
                "sha256": _sha256(observed),
                "size": len(observed),
                "errorType": type(exc).__name__,
            }
        raise LifecycleOperationLocationError(
            "operation-location-adoption-interrupted",
            "the root copy is proven but the exact legacy source was not retired",
            expected={**item.model_dump(mode="json"), "state": "absent"},
            observed=observed_payload,
        ) from exc


def _publish_adoption_bytes(
    target: Path,
    payload: bytes,
    item: AdoptedLifecycleArtifact,
) -> None:
    error: OSError | None = None
    try:
        atomic_write_bytes(target, payload)
    except OSError as exc:
        error = exc
    try:
        observed = _read_bytes(target, "root operation")
    except LifecycleOperationLocationError as exc:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-interrupted",
            "the exact legacy operation was not proven at the canonical root",
            expected=item.model_dump(mode="json"),
            observed={"path": target.as_posix(), "errorType": type(error or exc).__name__},
        ) from error or exc
    _require_artifact_bytes(item, observed, side="root")


def _publish_adoption_receipt(path: Path, text: str) -> None:
    expected = text.encode("utf-8")
    error: OSError | None = None
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        error = exc
    try:
        observed = _read_bytes(path, "adoption receipt")
    except LifecycleOperationLocationError as exc:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-interrupted",
            "the addressable enclosure lacks exact adoption-receipt readback proof",
            expected={"path": path.as_posix(), "sha256": _sha256(expected)},
            observed={"errorType": type(error or exc).__name__},
        ) from error or exc
    if observed != expected:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-conflict",
            "the adoption receipt bytes differ from the accepted request",
            expected={"path": path.as_posix(), "sha256": _sha256(expected)},
            observed={"sha256": _sha256(observed)},
        )


def _read_bytes(path: Path, owner: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LifecycleOperationLocationError(
            "operation-location-adoption-invalid",
            f"the explicit {owner} bytes are unreadable",
            expected={"path": path.as_posix()},
            observed={"errorType": type(exc).__name__},
        ) from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _adoption_request_id(
    artifacts: EnclosurePublicationArtifacts,
    intent: str,
    adopted: list[AdoptedLifecycleArtifact],
) -> str:
    payload = {
        "enclosurePublicationRequestId": (artifacts.reserved_locator.publicationRequestId),
        "auditIntent": intent,
        "contractSha256": artifacts.reserved_locator.expectedInitialContractSha256,
        "manifestSha256": artifacts.reserved_locator.expectedManifestSha256,
        "artifacts": [item.model_dump(mode="json") for item in adopted],
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
