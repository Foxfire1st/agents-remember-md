"""Bounded atomic storage for exact content-addressed certification objects."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Never, TypeVar

from pydantic import Field

from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    FinalizationCertificateAuthority,
    GateCertificate,
)
from agents_remember.certification.frozen_run.authorities import CandidateAuthorityRecords
from agents_remember.certification.frozen_run.models import FrozenCertificationRun
from agents_remember.certification.lifecycle_models import (
    CertificationRecoveryRecord,
    LifecycleAdmissionManifest,
    PriorRedDispositionManifest,
)
from agents_remember.certification.models import CertificationContractFinding, GateResultManifest
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.atomic_write import atomic_write_bytes
from agents_remember.kernel.file_lock import exclusive_file_lock
from agents_remember.models.certification.base import FrozenContractModel, SemanticText
from agents_remember.models.certification.references import (
    CertificateObjectKind,
    CertificateObjectReference,
)
from agents_remember.models.lifecycles.preparation import (
    CloseoutPreparationIntent,
    PreparedCloseoutOutput,
)

CertificateObject = (
    CertificationAdmissionManifest
    | GateResultManifest
    | GateCertificate
    | FinalizationCertificateAuthority
    | FrozenCertificationRun
    | CandidateAuthorityRecords
    | LifecycleAdmissionManifest
    | PriorRedDispositionManifest
    | CertificationRecoveryRecord
    | CloseoutPreparationIntent
    | PreparedCloseoutOutput
)
CertificateObjectT = TypeVar("CertificateObjectT", bound=CertificateObject)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KINDS: tuple[CertificateObjectKind, ...] = (
    "admission",
    "result-manifest",
    "certificate",
    "finalization",
    "frozen-run",
    "candidate-authorities",
    "lifecycle-admission",
    "prior-red-disposition",
    "recovery",
    "preparation-intent",
    "prepared-output",
)
_MODELS: dict[CertificateObjectKind, type[CertificateObject]] = {
    "admission": CertificationAdmissionManifest,
    "result-manifest": GateResultManifest,
    "certificate": GateCertificate,
    "finalization": FinalizationCertificateAuthority,
    "frozen-run": FrozenCertificationRun,
    "candidate-authorities": CandidateAuthorityRecords,
    "lifecycle-admission": LifecycleAdmissionManifest,
    "prior-red-disposition": PriorRedDispositionManifest,
    "recovery": CertificationRecoveryRecord,
    "preparation-intent": CloseoutPreparationIntent,
    "prepared-output": PreparedCloseoutOutput,
}


class CertificateStorePolicy(FrozenContractModel):
    """Operation-scoped capacity and the existing owner that reclaims the store."""

    scopeId: SemanticText = Field(max_length=512)
    maxObjects: int = Field(gt=0, le=100_000)
    maxBytes: int = Field(gt=0, le=10_000_000_000)
    reclamationOwner: SemanticText = Field(max_length=512)


class ContentAddressedCertificateStore:
    """Publish immutable objects by exact digest without a latest-object lookup."""

    def __init__(self, root: Path, policy: CertificateStorePolicy) -> None:
        self._root = root
        self._policy = policy

    def publish(self, value: CertificateObject) -> Path:
        """Publish one exact registered model at its canonical kind and digest."""
        kind = _kind_for_model(type(value))
        return self._publish(kind, _object_digest(value), value)

    def load(self, model: type[CertificateObjectT], digest: str) -> CertificateObjectT:
        """Read an exact address using its registered model as the typed selector."""
        return self._load(_kind_for_model(model), digest, model)

    def reference(self, kind: CertificateObjectKind, digest: str) -> CertificateObjectReference:
        """Bind schema-validated readback bytes before the caller selects their reference."""

        self.exact_path(kind, digest)
        value = self._load(kind, digest, _MODELS[kind])
        raw = _canonical_bytes(value)
        return CertificateObjectReference(
            kind=kind,
            semanticDigest=digest,
            contentSha256=hashlib.sha256(raw).hexdigest(),
            sizeBytes=len(raw),
        )

    def load_reference(self, reference: CertificateObjectReference) -> CertificateObject:
        """Read only the selected semantic address and verify its complete original bytes."""

        value = self._load(reference.kind, reference.semanticDigest, _MODELS[reference.kind])
        raw = _canonical_bytes(value)
        if (
            len(raw) != reference.sizeBytes
            or hashlib.sha256(raw).hexdigest() != reference.contentSha256
        ):
            _raise(
                "certificate object reference refused",
                "certificate-object-reference-mismatch",
                self.exact_path(reference.kind, reference.semanticDigest).as_posix(),
                "stored canonical bytes do not match the selected original object reference",
            )
        return value

    def exact_path(self, kind: CertificateObjectKind, digest: str) -> Path:
        """Return the sole address for one kind/digest pair."""

        if not _DIGEST.fullmatch(digest):
            _raise(
                "certificate object lookup refused",
                "certificate-object-digest-invalid",
                "digest",
                "content-addressed lookup requires one exact lowercase SHA-256 digest",
            )
        if kind not in _KINDS:
            _raise(
                "certificate object lookup refused",
                "certificate-object-kind-invalid",
                "kind",
                "content-addressed lookup requires a declared certificate object kind",
            )
        return self._root / kind / "sha256" / digest[:2] / f"{digest}.json"

    def _publish(
        self,
        kind: CertificateObjectKind,
        digest: str,
        value: CertificateObject,
    ) -> Path:
        # One store-level mutex/flock also serializes capacity across different
        # addresses. The journal owner selects references only after this ends;
        # no other resource lock is acquired inside the publication transaction.
        with exclusive_file_lock(self._root / "publication", "certificate store publication"):
            return self._publish_locked(kind, digest, value)

    def _publish_locked(
        self,
        kind: CertificateObjectKind,
        digest: str,
        value: CertificateObject,
    ) -> Path:
        path = self.exact_path(kind, digest)
        payload = _canonical_bytes(value)
        existing = _read_regular_file(path, missing_ok=True)
        if existing is not None:
            if existing != payload:
                _raise(
                    "certificate object publication refused",
                    "content-address-collision",
                    path.as_posix(),
                    "an existing object at this exact digest has different bytes",
                )
            return path
        self._require_capacity(len(payload))
        atomic_write_bytes(path, payload)
        if _read_regular_file(path, missing_ok=False) != payload:
            _raise(
                "certificate object publication failed",
                "certificate-object-readback-mismatch",
                path.as_posix(),
                "atomic publication did not read back as the exact canonical bytes",
            )
        return path

    def _load(
        self,
        kind: CertificateObjectKind,
        digest: str,
        model: type[CertificateObjectT],
    ) -> CertificateObjectT:
        path = self.exact_path(kind, digest)
        raw = _read_regular_file(path, missing_ok=False)
        assert raw is not None
        try:
            payload = json.loads(raw)
            value = model.model_validate(payload)
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            _raise(
                "certificate object lookup refused",
                "certificate-object-invalid",
                path.as_posix(),
                f"stored object failed exact schema or digest validation: {type(error).__name__}",
            )
        observed_digest = _object_digest(value)
        if observed_digest != digest or _canonical_bytes(value) != raw:
            _raise(
                "certificate object lookup refused",
                "certificate-object-address-mismatch",
                path.as_posix(),
                "stored canonical bytes do not match the requested exact address",
            )
        return value

    def _require_capacity(self, additional_bytes: int) -> None:
        object_count = 0
        byte_count = 0
        for kind in _KINDS:
            root = self._root / kind / "sha256"
            if not root.exists():
                continue
            for path in root.glob("*/*.json"):
                object_count += 1
                if object_count >= self._policy.maxObjects:
                    _capacity_error(self._policy)
                try:
                    mode = path.lstat().st_mode
                    if not stat.S_ISREG(mode):
                        raise OSError("stored object is not a regular file")
                    byte_count += path.stat().st_size
                except OSError:
                    _raise(
                        "certificate store capacity check refused",
                        "certificate-store-object-invalid",
                        path.as_posix(),
                        "capacity cannot be proven while a stored object is unsafe or unreadable",
                    )
                if byte_count + additional_bytes > self._policy.maxBytes:
                    _capacity_error(self._policy)
        if byte_count + additional_bytes > self._policy.maxBytes:
            _capacity_error(self._policy)


def _canonical_bytes(value: CertificateObject) -> bytes:
    payload = value.model_dump(mode="json")
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _kind_for_model(model: type[CertificateObject]) -> CertificateObjectKind:
    for kind, registered in _MODELS.items():
        if model is registered:
            return kind
    _raise(
        "certificate object model refused",
        "certificate-object-model-invalid",
        "model",
        f"content-addressed storage requires an exact registered model, not {model.__name__}",
    )


def _object_digest(value: CertificateObject) -> str:
    if isinstance(value, (CertificationAdmissionManifest, LifecycleAdmissionManifest)):
        digest = value.admissionDigest
    elif isinstance(value, GateResultManifest):
        digest = value.manifestDigest
    elif isinstance(value, GateCertificate):
        digest = value.certificateDigest
    elif isinstance(value, FrozenCertificationRun):
        digest = value.runDigest
    elif isinstance(value, PriorRedDispositionManifest):
        digest = value.dispositionDigest
    elif isinstance(value, CertificationRecoveryRecord):
        digest = value.recoveryDigest
    elif isinstance(value, CloseoutPreparationIntent):
        digest = value.intentDigest
    elif isinstance(value, PreparedCloseoutOutput):
        digest = value.outputDigest
    else:
        digest = value.authorityDigest
    return digest


def _read_regular_file(path: Path, *, missing_ok: bool) -> bytes | None:
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise OSError("object is not a regular file")
        return path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None
        _raise(
            "certificate object lookup refused",
            "certificate-object-missing",
            path.as_posix(),
            "the exact content-addressed object does not exist",
        )
    except OSError as error:
        _raise(
            "certificate object lookup refused",
            "certificate-object-unsafe",
            path.as_posix(),
            f"the exact object is not a safe readable regular file: {type(error).__name__}",
        )


def _capacity_error(policy: CertificateStorePolicy) -> None:
    _raise(
        "certificate store capacity exceeded",
        "certificate-store-capacity-exceeded",
        policy.scopeId,
        (
            "operation-scoped object limits were reached; preserve required evidence and invoke "
            f"the declared reclamation owner {policy.reclamationOwner}"
        ),
    )


def _raise(detail: str, code: str, path: str, finding_detail: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=finding_detail)
    raise CertificationContractError(detail, (finding.model_dump(mode="json"),))


__all__ = [
    "CertificateStorePolicy",
    "ContentAddressedCertificateStore",
]
