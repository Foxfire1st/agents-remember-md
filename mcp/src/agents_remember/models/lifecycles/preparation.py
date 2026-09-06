"""Closed records for unfinished private closeout preparation, never delivery proof."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from agents_remember.models.certification.base import FrozenContractModel, SemanticText
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256

_GIT_OBJECT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_GitObject = Annotated[str, Field(pattern=_GIT_OBJECT_PATTERN)]
_Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_PathText = Annotated[str, Field(min_length=1, max_length=8192)]
_MAX_COMMIT_BYTES = 8_388_608

PreparationLeg = Literal["code", "memory-content", "ledger"]
PreparationHookPolicy = Literal["strict-code-no-verify", "ordinary"]


def _canonical_preparation_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or path.as_posix() != value
        or ".." in path.parts
        or "\x00" in value
    ):
        raise ValueError("preparation paths must have canonical absolute path spelling")
    return value


class ExistingMemoryPreparationProof(FrozenContractModel):
    """Exact memory reuse observations; physical Git and mapping proof stay upstream.

    The ledger blob names memory.md at logicalHeadCommit. Mapped content can
    precede that head; an explicitly unmapped route uses that exact head. The
    owner proves mapping presence/absence, ancestry and equal non-ledger entries;
    this record only binds their observed identities.
    """

    schemaVersion: Literal["existing-memory-preparation-proof/v1"] = (
        "existing-memory-preparation-proof/v1"
    )
    repositoryIdentity: _PathText
    logicalHeadCommit: _GitObject
    logicalHeadTree: _GitObject
    ledgerBlob: _GitObject
    codeCommit: _GitObject
    mappingDisposition: Literal["existing-mapping", "unmapped-head"]
    memoryContentCommit: _GitObject
    memoryContentTree: _GitObject
    nonLedgerEntriesSha256: _Digest
    proofDigest: _Digest

    @field_validator("repositoryIdentity")
    @classmethod
    def _require_repository_path(cls, value: str) -> str:
        return _canonical_preparation_path(value)

    @model_validator(mode="after")
    def _require_proof(self) -> Self:
        memory_objects = (
            self.logicalHeadCommit,
            self.logicalHeadTree,
            self.ledgerBlob,
            self.memoryContentCommit,
            self.memoryContentTree,
        )
        if len({len(value) for value in memory_objects}) != 1:
            raise ValueError("existing memory Git identities must use one object format")
        if self.mappingDisposition == "unmapped-head" and (
            self.memoryContentCommit,
            self.memoryContentTree,
        ) != (self.logicalHeadCommit, self.logicalHeadTree):
            raise ValueError("unmapped memory reuse requires the exact logical head and tree")
        if self.proofDigest != canonical_sha256(
            self.model_dump(mode="json", exclude={"proofDigest"})
        ):
            raise ValueError("existing memory proof digest must bind the complete record")
        return self


class CloseoutPreparationIntent(FrozenContractModel):
    """The exact private work selected by one operation before a Git command starts.

    Reference kind/byte shape is checked here. The lifecycle owner must separately
    load the selected originals and prove a complete applicable green code prefix,
    enabled-leg authority, physical paths, configuration and current worker lease.
    """

    schemaVersion: Literal["closeout-preparation-intent/v1"] = "closeout-preparation-intent/v1"
    operationKey: _Digest
    generation: int = Field(strict=True, ge=1)
    contractPath: _PathText
    contractSha256: _Digest
    leg: PreparationLeg
    writeEnabled: bool = Field(strict=True)
    repositoryIdentity: _PathText
    logicalRoot: _PathText
    logicalRef: str = Field(min_length=12, max_length=4096)
    expectedOldCommit: _GitObject
    parentCommit: _GitObject
    admittedTree: _GitObject
    privateRoot: _PathText | None
    normalizedMessage: Annotated[SemanticText, Field(max_length=1_048_576)] | None
    hookPolicy: PreparationHookPolicy
    gitConfigSha256: _Digest
    hooksSha256: _Digest
    frozenRun: CertificateObjectReference
    candidateAuthorities: CertificateObjectReference
    prefixCertificates: tuple[CertificateObjectReference, ...] = Field(min_length=1, max_length=4)
    gateFiveCertificate: CertificateObjectReference | None
    existingMemoryProof: ExistingMemoryPreparationProof | None = None
    intentDigest: _Digest

    @field_validator("contractPath", "repositoryIdentity", "logicalRoot", "privateRoot")
    @classmethod
    def _require_path_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_preparation_path(value)

    @field_validator("logicalRef")
    @classmethod
    def _require_branch_ref(cls, value: str) -> str:
        components = value.split("/")
        if (
            not value.startswith("refs/heads/")
            or any(
                not part or part.startswith(".") or part.endswith(".lock") for part in components
            )
            or value.endswith(".")
            or ".." in value
            or "@{" in value
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
        ):
            raise ValueError("preparation logical ref must be one exact local branch ref")
        return value

    @field_validator("normalizedMessage")
    @classmethod
    def _require_message_bytes(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("preparation commit message cannot contain NUL")
        return value

    @model_validator(mode="after")
    def _require_intent(self) -> Self:
        expected_policy = "strict-code-no-verify" if self.leg == "code" else "ordinary"
        if self.hookPolicy != expected_policy:
            raise ValueError("preparation hook policy must match its declared leg")
        _require_preparation_route(self)
        _require_preparation_reuse(self)
        if len({len(self.expectedOldCommit), len(self.parentCommit), len(self.admittedTree)}) != 1:
            raise ValueError("preparation Git object identities must use one object format")
        _require_preparation_references(self)
        if self.intentDigest != canonical_sha256(
            self.model_dump(mode="json", exclude={"intentDigest"})
        ):
            raise ValueError("preparation intent digest must bind the complete record")
        return self


def _require_preparation_route(intent: CloseoutPreparationIntent) -> None:
    if not intent.writeEnabled:
        if intent.privateRoot is not None or intent.normalizedMessage is not None:
            raise ValueError("no-write preparation requires no private path or message")
        if intent.leg == "code" and intent.parentCommit != intent.expectedOldCommit:
            raise ValueError("existing code preparation requires its exact logical parent")
        return
    if intent.privateRoot is None or intent.normalizedMessage is None:
        raise ValueError("enabled preparation requires a real private path and normalized message")
    private = PurePosixPath(intent.privateRoot)
    for protected in (intent.logicalRoot, intent.repositoryIdentity):
        other = PurePosixPath(protected)
        if private.is_relative_to(other) or other.is_relative_to(private):
            raise ValueError(
                "private preparation must be separate from logical checkout and Git metadata"
            )


def _require_preparation_reuse(intent: CloseoutPreparationIntent) -> None:
    proof = intent.existingMemoryProof
    if intent.leg == "code":
        if proof is not None:
            raise ValueError("code preparation cannot carry an existing memory proof")
        return
    if proof is None:
        if not intent.writeEnabled:
            raise ValueError("no-write memory preparation requires an exact existing memory proof")
        return
    if (intent.repositoryIdentity, intent.expectedOldCommit) != (
        proof.repositoryIdentity,
        proof.logicalHeadCommit,
    ):
        raise ValueError("existing memory proof differs from the intended repository or head")
    if intent.writeEnabled:
        if intent.leg != "ledger" or intent.parentCommit != proof.logicalHeadCommit:
            raise ValueError(
                "created ledger after reused memory must parent the exact logical head"
            )
        return
    if intent.leg == "ledger" and proof.mappingDisposition != "existing-mapping":
        raise ValueError("existing ledger preparation requires an exact existing mapping")
    parent, tree = (
        (proof.memoryContentCommit, proof.memoryContentTree)
        if intent.leg == "memory-content"
        else (proof.logicalHeadCommit, proof.logicalHeadTree)
    )
    if (intent.parentCommit, intent.admittedTree) != (parent, tree):
        raise ValueError("existing memory preparation must retain its exact leg commit and tree")


def _require_preparation_references(intent: CloseoutPreparationIntent) -> None:
    for reference, kind in (
        (intent.frozenRun, "frozen-run"),
        (intent.candidateAuthorities, "candidate-authorities"),
    ):
        if reference.kind != kind:
            raise ValueError(f"preparation requires an exact {kind} reference")
    if any(reference.kind != "certificate" for reference in intent.prefixCertificates):
        raise ValueError("preparation prefix contains a non-certificate reference")
    if len(set(intent.prefixCertificates)) != len(intent.prefixCertificates):
        raise ValueError("preparation prefix references must be unique")
    if intent.leg == "code":
        if intent.gateFiveCertificate is not None:
            raise ValueError("code preparation must precede Gate-5 certification")
    elif intent.gateFiveCertificate is None or intent.gateFiveCertificate.kind != "certificate":
        raise ValueError("memory and ledger preparation require a Gate-5 certificate reference")


class PreparedCloseoutOutput(FrozenContractModel):
    """Observed real Git object bytes retained before protected task publication.

    Object validity is not proof of its existence in a repository, a clean private
    checkout, current authority, Gate 5, approval consumption or a published leg.
    Those facts remain the preparation/publication owner's responsibility.
    """

    schemaVersion: Literal["prepared-closeout-output/v1"] = "prepared-closeout-output/v1"
    intent: CertificateObjectReference
    disposition: Literal["created", "existing"]
    commit: _GitObject
    tree: _GitObject
    parents: tuple[_GitObject, ...] = Field(max_length=64)
    committerDate: str = Field(min_length=1, max_length=64)
    authorIdentity: SemanticText = Field(max_length=8192)
    committerIdentity: SemanticText = Field(max_length=8192)
    rawCommitBase64: str = Field(min_length=1, max_length=4 * ((_MAX_COMMIT_BYTES + 2) // 3))
    rawCommitSha256: _Digest
    messageSha256: _Digest
    signaturePresent: bool = Field(strict=True)
    outputDigest: _Digest

    @model_validator(mode="after")
    def _require_output(self) -> Self:
        if self.intent.kind != "preparation-intent":
            raise ValueError("prepared output requires an exact preparation-intent reference")
        if len({len(item) for item in (self.commit, self.tree, *self.parents)}) != 1:
            raise ValueError("prepared Git object identities must use one object format")
        raw = _raw_commit(self.rawCommitBase64)
        if hashlib.sha256(raw).hexdigest() != self.rawCommitSha256:
            raise ValueError("prepared raw commit bytes differ from their SHA-256")
        if _commit_object_id(raw, self.commit) != self.commit:
            raise ValueError("prepared commit identity differs from its raw Git object")
        headers, message = _commit_headers(raw)
        _require_output_headers(self, headers, message)
        if self.outputDigest != canonical_sha256(
            self.model_dump(mode="json", exclude={"outputDigest"})
        ):
            raise ValueError("prepared output digest must bind the complete record")
        return self


def build_prepared_closeout_output(
    raw_commit: bytes,
    intent: CertificateObjectReference,
    *,
    disposition: Literal["created", "existing"],
) -> PreparedCloseoutOutput:
    """Derive a closed observation from exact Git bytes, without granting authority.

    The raw tree identity selects the existing Git object format. The caller
    separately proves repository existence and the selected intent relationship.
    """
    if not isinstance(raw_commit, bytes) or len(raw_commit) > _MAX_COMMIT_BYTES:
        raise ValueError("prepared raw commit must be bounded exact bytes")
    headers, message = _commit_headers(raw_commit)
    tree = _one_header(headers, "tree")
    committer = _one_header(headers, "committer")
    payload = {
        "schemaVersion": "prepared-closeout-output/v1",
        "intent": intent.model_dump(mode="json"),
        "disposition": disposition,
        "commit": _commit_object_id(raw_commit, tree),
        "tree": tree,
        "parents": [parent.decode("ascii") for parent in headers.get("parent", [])],
        "committerDate": _identity_date(committer),
        "authorIdentity": _one_header(headers, "author"),
        "committerIdentity": committer,
        "rawCommitBase64": base64.b64encode(raw_commit).decode("ascii"),
        "rawCommitSha256": hashlib.sha256(raw_commit).hexdigest(),
        "messageSha256": hashlib.sha256(message).hexdigest(),
        "signaturePresent": "gpgsig" in headers or "gpgsig-sha256" in headers,
    }
    return PreparedCloseoutOutput.model_validate(
        {**payload, "outputDigest": canonical_sha256(payload)}
    )


def _commit_object_id(raw: bytes, format_identity: str) -> str:
    if re.fullmatch(_GIT_OBJECT_PATTERN, format_identity) is None:
        raise ValueError("prepared Git object identity must use the supported object format")
    framed = f"commit {len(raw)}\0".encode("ascii") + raw
    if len(format_identity) == 40:
        return hashlib.sha1(framed, usedforsecurity=False).hexdigest()
    return hashlib.sha256(framed).hexdigest()


def _raw_commit(encoded: str) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("prepared raw commit must be canonical base64") from error
    if len(raw) > _MAX_COMMIT_BYTES or base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError("prepared raw commit must be bounded canonical base64")
    return raw


def _commit_headers(raw: bytes) -> tuple[dict[str, list[bytes]], bytes]:
    header, separator, message = raw.partition(b"\n\n")
    if not separator or b"\0" in raw:
        raise ValueError("prepared raw commit must contain headers and message without NUL")
    headers: dict[str, list[bytes]] = {}
    current: str | None = None
    for line in header.split(b"\n"):
        if line.startswith(b" "):
            if current is None or current in {"tree", "parent", "author", "committer"}:
                raise ValueError("prepared raw commit has an invalid continued header")
            headers[current][-1] += b"\n" + line
            continue
        key, delimiter, value = line.partition(b" ")
        if not delimiter or not re.fullmatch(rb"[a-z][a-z0-9-]*", key):
            raise ValueError("prepared raw commit has an invalid header")
        current = key.decode("ascii")
        headers.setdefault(current, []).append(value)
    return headers, message


def _one_header(headers: dict[str, list[bytes]], name: str) -> str:
    values = headers.get(name, [])
    if len(values) != 1:
        raise ValueError(f"prepared raw commit requires one {name} header")
    try:
        return values[0].decode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"prepared {name} header must be UTF-8") from error


def _identity_date(identity: str) -> str:
    matched = re.fullmatch(r"[^<>\n]+ <[^<>\n]+> (-?[0-9]+) ([+-])([0-9]{2})([0-9]{2})", identity)
    if matched is None:
        raise ValueError("prepared identity must retain the exact Git name, email and timestamp")
    seconds, sign, hours, minutes = matched.groups()
    if int(hours) > 23 or int(minutes) > 59:
        raise ValueError("prepared identity has an invalid Git timezone")
    offset = timedelta(hours=int(hours), minutes=int(minutes)) * (-1 if sign == "-" else 1)
    try:
        return datetime.fromtimestamp(int(seconds), timezone(offset)).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(
            "prepared identity timestamp is outside supported datetime bounds"
        ) from error


def _require_output_headers(
    output: PreparedCloseoutOutput, headers: dict[str, list[bytes]], message: bytes
) -> None:
    if _one_header(headers, "tree") != output.tree:
        raise ValueError("prepared tree differs from its raw commit")
    if headers.get("parent", []) != [parent.encode("ascii") for parent in output.parents]:
        raise ValueError("prepared parents differ from their raw commit")
    for field, actual in (
        ("author", output.authorIdentity),
        ("committer", output.committerIdentity),
    ):
        if _one_header(headers, field) != actual:
            raise ValueError(f"prepared {field} identity differs from its raw commit")
        stamp = _identity_date(actual)
        if field == "committer" and stamp != output.committerDate:
            raise ValueError("prepared committer date differs from its raw commit")
    if hashlib.sha256(message).hexdigest() != output.messageSha256:
        raise ValueError("prepared message bytes differ from their SHA-256")
    signed = "gpgsig" in headers or "gpgsig-sha256" in headers
    if signed != output.signaturePresent:
        raise ValueError("prepared signature presence differs from its raw commit")


def require_prepared_output_matches_intent(
    output: PreparedCloseoutOutput, intent: CloseoutPreparationIntent
) -> None:
    """Validate the selected intent's exact bytes and object relation, without Git I/O."""
    output = PreparedCloseoutOutput.model_validate(output.model_dump(mode="json"))
    intent = CloseoutPreparationIntent.model_validate(intent.model_dump(mode="json"))
    raw = (
        json.dumps(
            intent.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    reference = output.intent
    if (
        reference.semanticDigest != intent.intentDigest
        or reference.contentSha256 != hashlib.sha256(raw).hexdigest()
        or reference.sizeBytes != len(raw)
    ):
        raise ValueError("prepared output must retain its exact selected intent reference")
    if output.tree != intent.admittedTree:
        raise ValueError("prepared output tree differs from its admitted intent")
    if output.disposition == "created":
        if (
            not intent.writeEnabled
            or output.parents != (intent.parentCommit,)
            or output.commit == intent.parentCommit
        ):
            raise ValueError(
                "created output requires enabled preparation at its exact intended parent"
            )
    elif intent.writeEnabled or output.commit != intent.parentCommit:
        raise ValueError("existing output requires the exact no-write intent")
