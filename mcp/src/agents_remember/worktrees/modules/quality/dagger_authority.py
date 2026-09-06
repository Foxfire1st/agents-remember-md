"""One universal host-level Dagger runner/layer-store execution authority.

CCR-R12 requires every Dagger-backed execution -- closeout, standalone quality,
integration, retry, recovery, lifecycle continuation, and future repository-profile
routes -- to cross one trusted host launch boundary that admits exactly one
connection-only runner endpoint and one exact reusable layer-store identity. This
module owns that boundary:

* strict host declaration parsing (missing / malformed / unsupported /
  provisioning-capable / worktree-local / ambiguous all refuse with typed defects
  before any Dagger command starts);
* live engine + layer-store inspection of the already-running runner;
* an immutable frozen authority snapshot whose safe digest binds the operation
  fingerprint, candidate, generation, plan, manifest, result, certificate, retry key,
  recovery equality, and integration attestation;
* a host-level locked cross-operation owner registry with process-start/generation
  liveness so terminalization releases only the exact owner and crash recovery
  reconciles stale ownership without rewriting primary results;
* a typed authority-transition barrier: a changed host declaration while the old
  authority still has live owners refuses every fresh admission and never lets old
  and new runner/store authorities serve live operations concurrently.

There is deliberately no private runner, per-worktree store, profile override,
compatibility alias, repository special case, or fallback here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Never, Protocol
from urllib.parse import urlparse

from agents_remember.errors import DaggerRuntimeAuthorityError, LockCapabilityError
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.kernel.file_lock import exclusive_file_lock

HOST_DECLARATION_ENV = "AR_DAGGER_RUNTIME_AUTHORITY"
HOST_REGISTRY_ROOT_ENV = "AR_DAGGER_AUTHORITY_ROOT"
RUNTIME_AUTHORITY_DIGEST_ENV = "AR_DAGGER_RUNTIME_AUTHORITY_DIGEST"
DAGGER_HOST_ENV = "DAGGER_HOST"
DEFAULT_LAYER_STORE = "/var/lib/dagger"
DEFAULT_ENGINE_VERSION = "v0.21.8"
AUTHORITY_SCHEMA_VERSION = "dagger-host-declaration/v1"
SNAPSHOT_SCHEMA_VERSION = "dagger-runtime-authority/v1"
STATE_SCHEMA_VERSION = "dagger-authority-state/v1"
OWNERS_SCHEMA_VERSION = "dagger-authority-owners/v1"

# Connection-only URI schemes. docker-container and docker-image would let the
# engine provision a fresh container/volume; local/auto/empty start the CLI's
# default engine and an anonymous volume. None of those may name the admitted runner.
_CONNECTION_ONLY_SCHEMES = frozenset({"container", "tcp", "unix", "grpc"})
_PROVISIONING_SCHEMES = frozenset(
    {"docker-container", "docker-image", "local", "auto", "dagger", "start"}
)

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_LOCK_STORE = "dagger-authority-owners"


def _sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DaggerHostDeclaration:
    """One host-owned connection-only runner and reusable layer-store selection."""

    schema_version: Literal["dagger-host-declaration/v1"]
    endpoint: str
    layer_store: str
    engine_version: str
    source: str

    @property
    def declaration_digest(self) -> str:
        """Safe identity of the declaration bytes alone (not the live inspection)."""
        return _sha256(
            {
                "schemaVersion": self.schema_version,
                "endpoint": self.endpoint,
                "layerStore": self.layer_store,
                "engineVersion": self.engine_version,
            }
        )

    def as_manifest(self) -> dict[str, str]:
        return {
            "schemaVersion": self.schema_version,
            "endpoint": self.endpoint,
            "layerStore": self.layer_store,
            "engineVersion": self.engine_version,
        }


@dataclass(frozen=True)
class InspectedRuntime:
    """Facts verified about the actual already-running engine and store mount."""

    engine_id: str
    engine_running: bool
    store_mounted_path: str
    store_source: str
    observed_version: str | None


@dataclass(frozen=True)
class DaggerAuthoritySnapshot:
    """Immutable frozen authority one operation may launch against."""

    schema_version: Literal["dagger-runtime-authority/v1"]
    endpoint: str
    layer_store: str
    engine_version: str
    engine_id: str
    store_mounted_path: str
    store_source: str
    observed_version: str | None
    inspected_at: str
    snapshot_digest: str

    def as_manifest(self) -> dict[str, str]:
        return {
            "schemaVersion": self.schema_version,
            "snapshotDigest": self.snapshot_digest,
            "endpoint": self.endpoint,
            "layerStore": self.layer_store,
            "engineId": self.engine_id,
            "engineVersion": self.engine_version,
            "storeMountedPath": self.store_mounted_path,
        }


@dataclass(frozen=True)
class DaggerOwner:
    """Cross-operation owner identity for one live Dagger consumer."""

    owner_id: str
    authority_digest: str
    operation_kind: str
    scope: str
    generation: str
    pid: int
    process_fingerprint: str
    started_at: str


@dataclass(frozen=True)
class AdmittedDaggerAuthority:
    """The admitted launch context: frozen snapshot plus the deterministic env."""

    snapshot: DaggerAuthoritySnapshot
    declaration: DaggerHostDeclaration
    environment: Mapping[str, str]
    owner: DaggerOwner | None = None

    @property
    def snapshot_digest(self) -> str:
        return self.snapshot.snapshot_digest


@dataclass(frozen=True)
class AuthorityState:
    """Persisted host state: the active authority plus any transition barrier."""

    schema_version: Literal["dagger-authority-state/v1"]
    active_digest: str | None
    activated_at: str | None
    barrier_pending_digest: str | None
    barrier_entered_at: str | None


@dataclass(frozen=True)
class AuthorityBarrier:
    """One typed authority-transition barrier with the live-owner census."""

    pending_digest: str
    active_digest: str | None
    live_owner_census: int


class EngineInspectionProtocol(Protocol):
    def inspect(self, declaration: DaggerHostDeclaration) -> InspectedRuntime: ...


class DockerEngineInspector:
    """Default production inspector: docker inspect against the named engine.

    The declared endpoint names one already-running engine container plus the exact
    reusable store mounted at its layer-store location. Inspection is connection-only:
    this class never creates, starts, or provisions an engine or volume.
    """

    def __init__(self, *, docker_executable: str = "docker") -> None:
        self.docker_executable = docker_executable

    def inspect(self, declaration: DaggerHostDeclaration) -> InspectedRuntime:
        engine = _endpoint_engine_name(declaration.endpoint)
        self._require_running_engine(engine)
        store_mount = self._require_layer_store(engine, declaration.layer_store)
        observed_version = self._observed_engine_version(engine)
        return InspectedRuntime(
            engine_id=engine,
            engine_running=True,
            store_mounted_path=store_mount["destination"],
            store_source=store_mount["source"],
            observed_version=observed_version,
        )

    def _require_running_engine(self, engine: str) -> None:
        """Prove the declared engine container exists and is running (typed defects)."""
        try:
            running = subprocess.run(
                [
                    self.docker_executable,
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    engine,
                ],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except OSError as error:
            raise DaggerRuntimeAuthorityError(
                "runtime authority inspection could not start",
                (
                    _finding(
                        "runtime-authority-inspection-failed",
                        "engine",
                        f"docker inspection of {engine!r} is unavailable: {error}",
                    ),
                ),
            ) from error
        if running.returncode != 0:
            raise DaggerRuntimeAuthorityError(
                "runtime authority engine is not an existing engine",
                (
                    _finding(
                        "runtime-authority-inspection-failed",
                        "engine",
                        f"engine {engine!r} is not an existing running engine",
                    ),
                ),
            )
        if running.stdout.strip() != "true":
            raise DaggerRuntimeAuthorityError(
                "runtime authority engine is not running",
                (
                    _finding(
                        "runtime-authority-inspection-failed",
                        "engine",
                        f"engine {engine!r} is not running",
                    ),
                ),
            )

    def _require_layer_store(self, engine: str, expected_store: str) -> dict[str, str]:
        """Return the mounted store source/destination or refuse with a typed defect."""
        mounts = subprocess.run(
            [
                self.docker_executable,
                "inspect",
                "--format",
                "{{json .Mounts}}",
                engine,
            ],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if mounts.returncode != 0:
            raise DaggerRuntimeAuthorityError(
                "runtime authority store mount could not be inspected",
                (
                    _finding(
                        "runtime-authority-inspection-failed",
                        "layer-store",
                        f"store mount inspection of engine {engine!r} failed",
                    ),
                ),
            )
        store_mount = _find_store_mount(mounts.stdout, expected_store)
        if store_mount is None:
            raise DaggerRuntimeAuthorityError(
                "runtime authority layer store is not mounted at its declared location",
                (
                    _finding(
                        "runtime-authority-inspection-failed",
                        "layer-store",
                        (
                            f"engine {engine!r} does not mount the reusable layer store at "
                            f"{expected_store}"
                        ),
                    ),
                ),
            )
        return store_mount

    def _observed_engine_version(self, engine: str) -> str | None:
        """Best-effort engine CLI version; absence never refuses the authority."""
        version = subprocess.run(
            [self.docker_executable, "exec", engine, "dagger", "--version"],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return version.stdout.strip() if version.returncode == 0 else None


def _find_store_mount(raw_mounts: str, expected_destination: str) -> dict[str, str] | None:
    mounts = _parse_store_mounts(raw_mounts)
    if mounts is None:
        return None
    return _store_mount_entry(mounts, expected_destination)


def _parse_store_mounts(raw_mounts: str) -> list[object] | None:
    try:
        parsed = json.loads(raw_mounts)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


def _store_mount_entry(
    mounts: list[object],
    expected_destination: str,
) -> dict[str, str] | None:
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        entry = _typed_store_mount(mount, expected_destination)
        if entry is not None:
            return entry
    return None


def _typed_store_mount(mount: dict, expected_destination: str) -> dict[str, str] | None:
    destination = mount.get("Destination")
    if not isinstance(destination, str) or destination != expected_destination:
        return None
    source = mount.get("Source")
    if not isinstance(source, str):
        return None
    return {"destination": destination, "source": source}


def _endpoint_engine_name(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "container" or not parsed.netloc or "." in parsed.netloc:
        raise DaggerRuntimeAuthorityError(
            "runtime authority is not a connection-only container endpoint",
            (
                _finding(
                    "runtime-authority-malformed",
                    "endpoint",
                    f"{endpoint!r} does not name one engine container",
                ),
            ),
        )
    return parsed.netloc


def parse_host_declaration(
    raw: object,
    *,
    source: str,
    excluded_roots: Sequence[Path] = (),
) -> DaggerHostDeclaration:
    """Strictly parse one host declaration or refuse with a typed defect."""
    payload = _declaration_object(raw)
    _require_host_level_root(source, excluded_roots)
    endpoint = _endpoint_value(payload)
    return DaggerHostDeclaration(
        schema_version=AUTHORITY_SCHEMA_VERSION,  # type: ignore[arg-type]
        endpoint=endpoint,
        layer_store=_layer_store_value(payload),
        engine_version=_engine_version_value(payload),
        source=source,
    )


def _require_host_level_root(source: str, excluded_roots: Sequence[Path]) -> None:
    """Refuse a declaration that lives inside a repository or worktree."""
    if not excluded_roots:
        return
    source_path = Path(source)
    try:
        resolved_source = source_path.resolve()
    except OSError:
        resolved_source = source_path
    for root in excluded_roots:
        if not root:
            continue
        try:
            resolved_root = Path(root).resolve()
        except OSError:
            resolved_root = Path(root)
        if resolved_source.is_relative_to(resolved_root) or resolved_root.is_relative_to(
            resolved_source
        ):
            _raise_authority(
                "runtime-authority-worktree-local",
                "declaration.source",
                (
                    "the host declaration lives inside a repository or worktree; it must be "
                    "host-level and outside every repository"
                ),
            )


def _declaration_object(raw: object) -> dict[str, object]:
    if raw is None:
        _raise_authority("runtime-authority-missing", "declaration", "no host declaration exists")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            _raise_authority(
                "runtime-authority-malformed",
                "declaration",
                f"host declaration is not valid JSON: {error}",
            )
    else:
        payload = raw
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        _raise_authority(
            "runtime-authority-malformed", "declaration", "declaration must be a JSON object"
        )
    if payload.get("schemaVersion") != AUTHORITY_SCHEMA_VERSION:
        _raise_authority(
            "runtime-authority-unsupported",
            "declaration.schemaVersion",
            f"unsupported host declaration schema {payload.get('schemaVersion')!r}",
        )
    return payload


def _endpoint_value(payload: dict[str, object]) -> str:
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        _raise_authority(
            "runtime-authority-malformed", "declaration.endpoint", "endpoint is invalid"
        )
    parsed = urlparse(endpoint)
    if parsed.scheme in _PROVISIONING_SCHEMES or not parsed.scheme:
        _raise_authority(
            "runtime-authority-provisioning-capable",
            "declaration.endpoint",
            (
                f"endpoint {endpoint!r} can provision a private engine or anonymous volume; "
                "only a connection-only endpoint may name the admitted runner"
            ),
        )
    if parsed.scheme not in _CONNECTION_ONLY_SCHEMES:
        _raise_authority(
            "runtime-authority-unsupported",
            "declaration.endpoint",
            f"endpoint {endpoint!r} is not a supported connection-only scheme",
        )
    return endpoint


def _layer_store_value(payload: dict[str, object]) -> str:
    layer_store = payload.get("layerStore")
    if not isinstance(layer_store, str) or not layer_store or not layer_store.startswith("/"):
        _raise_authority(
            "runtime-authority-malformed",
            "declaration.layerStore",
            "layer store must be one absolute mounted path",
        )
    return layer_store


def _engine_version_value(payload: dict[str, object]) -> str:
    engine_version = payload.get("engineVersion")
    if (
        not isinstance(engine_version, str)
        or not engine_version
        or not engine_version.startswith("v")
    ):
        _raise_authority(
            "runtime-authority-malformed",
            "declaration.engineVersion",
            "engine version must be a pinned semver tag",
        )
    return engine_version


def authority_snapshot(
    declaration: DaggerHostDeclaration,
    inspected: InspectedRuntime,
    *,
    inspected_at: str,
) -> DaggerAuthoritySnapshot:
    """Freeze one immutable snapshot whose safe digest binds the operation."""
    payload = {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "endpoint": declaration.endpoint,
        "layerStore": declaration.layer_store,
        "engineVersion": declaration.engine_version,
        "engineId": inspected.engine_id,
        "storeMountedPath": inspected.store_mounted_path,
        "storeSource": inspected.store_source,
        "observedVersion": inspected.observed_version,
    }
    return DaggerAuthoritySnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,  # type: ignore[arg-type]
        endpoint=declaration.endpoint,
        layer_store=declaration.layer_store,
        engine_version=declaration.engine_version,
        engine_id=inspected.engine_id,
        store_mounted_path=inspected.store_mounted_path,
        store_source=inspected.store_source,
        observed_version=inspected.observed_version,
        inspected_at=inspected_at,
        snapshot_digest=_sha256(payload),
    )


def authority_environment(
    snapshot: DaggerAuthoritySnapshot,
    base: Mapping[str, str],
) -> dict[str, str]:
    """The deterministic environment from one admitted authority; refuse conflicts."""
    ambient = base.get(DAGGER_HOST_ENV)
    if ambient is not None and ambient.strip() and ambient != snapshot.endpoint:
        _raise_authority(
            "runtime-authority-ambient-conflict",
            DAGGER_HOST_ENV,
            (
                f"ambient DAGGER_HOST={ambient!r} conflicts with the admitted authority "
                f"endpoint {snapshot.endpoint!r}; the admitted authority is never merged "
                "with ambient or per-worktree selection"
            ),
        )
    env = dict(base)
    env[DAGGER_HOST_ENV] = snapshot.endpoint
    env[RUNTIME_AUTHORITY_DIGEST_ENV] = snapshot.snapshot_digest
    return env


def load_host_declaration(environ: Mapping[str, str] | None = None) -> DaggerHostDeclaration:
    """Load the sole host-level declaration from its one configured source."""
    resolved = os.environ if environ is None else environ
    raw = resolved.get(HOST_DECLARATION_ENV)
    if raw is None:
        _raise_authority(
            "runtime-authority-missing",
            "declaration",
            f"{HOST_DECLARATION_ENV} is not set; no host Dagger authority is declared",
        )
    if not isinstance(raw, str):
        _raise_authority(
            "runtime-authority-malformed",
            "declaration",
            "host declaration must be a JSON string",
        )
    return parse_host_declaration(raw, source=f"env:{HOST_DECLARATION_ENV}")


class AuthorityRegistry:
    """Host-level locked registry of live Dagger consumers plus the active authority.

    Writes are serialized with POSIX flock through exclusive_access. Owner
    liveness uses a process-start fingerprint, never PID alone, so a reused PID cannot
    release or suppress another live operation's owner.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "state.json"
        self.owners_path = root / "owners.json"
        self.lock_path = root / "authority.lock"

    @contextmanager
    def exclusive_access(self) -> Iterator[None]:
        """Serialize host ownership transactions independently of checkout coordination.

        Admission, continuation and release apply this registry's authority policy
        under the same physical lock. No control-plane process role is conferred.
        The resource remains authority.lock, so the shared primitive preserves the
        existing authority.lock.lock inode used by other registry processes.
        """
        try:
            with exclusive_file_lock(self.lock_path, _LOCK_STORE):
                yield
        except LockCapabilityError as error:
            raise DaggerRuntimeAuthorityError(
                "runtime authority registry cannot provide exclusive access",
                (_finding("runtime-authority-registry-lock-unsafe", "registry.lock", str(error)),),
            ) from error

    def state(self) -> AuthorityState:
        self._ensure_files()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            self._raise_corrupt("state", repr(error))
        if not isinstance(payload, dict) or payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
            self._raise_corrupt("state", "schema mismatch")
        active = payload.get("active")
        barrier = payload.get("barrier")
        active_digest: str | None = None
        activated_at: str | None = None
        if active is not None:
            if not isinstance(active, dict):
                self._raise_corrupt("state", "active")
            if not isinstance(active.get("snapshotDigest"), str):
                self._raise_corrupt("state", "active.snapshotDigest")
            active_digest = _digest_or_error(active.get("snapshotDigest"), "state.active")
            value = active.get("activatedAt")
            activated_at = value if isinstance(value, str) else None
        barrier_digest: str | None = None
        barrier_entered_at: str | None = None
        if barrier is not None:
            if not isinstance(barrier, dict) or barrier.get("state") != "awaiting-zero-census":
                self._raise_corrupt("state", "barrier")
            barrier_digest = _digest_or_error(barrier.get("pendingDigest"), "state.barrier")
            value = barrier.get("declaredAt")
            barrier_entered_at = value if isinstance(value, str) else None
        return AuthorityState(
            schema_version=STATE_SCHEMA_VERSION,
            active_digest=active_digest,
            activated_at=activated_at,
            barrier_pending_digest=barrier_digest,
            barrier_entered_at=barrier_entered_at,
        )

    def _write_state(self, state: AuthorityState) -> None:
        payload: dict[str, object] = {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "active": (
                None
                if state.active_digest is None
                else {"snapshotDigest": state.active_digest, "activatedAt": state.activated_at}
            ),
            "barrier": (
                None
                if state.barrier_pending_digest is None
                else {
                    "state": "awaiting-zero-census",
                    "pendingDigest": state.barrier_pending_digest,
                    "declaredAt": state.barrier_entered_at,
                }
            ),
        }
        atomic_write_text(self.state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _owners_payload(self) -> dict[str, object]:
        if not self.owners_path.exists():
            return {"schemaVersion": OWNERS_SCHEMA_VERSION, "owners": []}
        try:
            payload = json.loads(self.owners_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            self._raise_corrupt("owners", repr(error))
        if not isinstance(payload, dict) or payload.get("schemaVersion") != OWNERS_SCHEMA_VERSION:
            self._raise_corrupt("owners", "schema mismatch")
        return payload

    def owners(self) -> list[dict[str, object]]:
        payload = self._owners_payload()
        records = payload.get("owners")
        if not isinstance(records, list):
            self._raise_corrupt("owners", "records")
        return [record for record in records if isinstance(record, dict)]

    def _write_owners(self, records: Sequence[dict[str, object]]) -> None:
        atomic_write_text(
            self.owners_path,
            json.dumps(
                {"schemaVersion": OWNERS_SCHEMA_VERSION, "owners": list(records)},
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    def _ensure_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._write_state(
                AuthorityState(
                    schema_version=STATE_SCHEMA_VERSION,
                    active_digest=None,
                    activated_at=None,
                    barrier_pending_digest=None,
                    barrier_entered_at=None,
                )
            )
        if not self.owners_path.exists():
            self._write_owners([])

    def _raise_corrupt(self, surface: str, detail: object) -> Never:
        raise DaggerRuntimeAuthorityError(
            "runtime authority registry state is corrupt",
            (
                _finding(
                    "runtime-authority-registry-corrupt",
                    f"registry.{surface}",
                    f"durable registry {surface} is unreadable or invalid: {detail}",
                ),
            ),
        )

    # -- census / liveness ---------------------------------------------------

    def census(
        self,
        digest: str,
        liveness: Callable[[dict[str, object]], bool] | None = None,
    ) -> int:
        """Count live owners of one authority digest, reconciling stale records."""
        self.reconcile_stale(liveness)
        records = self.owners()
        return sum(
            1
            for record in records
            if record.get("authorityDigest") == digest and _record_live(record, liveness)
        )

    def reconcile_stale(self, liveness: Callable[[dict[str, object]], bool] | None = None) -> int:
        """Drop owners whose process-start fingerprint is no longer live (crash recovery).

        Reconciliation never rewrites an operation's primary result; it only removes
        stale owner rows so the census reflects currently live consumers.
        """
        records = self.owners()
        live = [record for record in records if _record_live(record, liveness)]
        dropped = len(records) - len(live)
        if dropped:
            self._write_owners(live)
        return dropped

    def live_owners(
        self, liveness: Callable[[dict[str, object]], bool] | None = None
    ) -> list[dict[str, object]]:
        return [record for record in self.owners() if _record_live(record, liveness)]

    # -- transition ----------------------------------------------------------

    def activate(self, digest: str, *, now: str) -> None:
        """Atomically activate one replacement authority and clear any barrier."""
        state = self.state()
        self._write_state(
            replace(
                state,
                active_digest=digest,
                activated_at=now,
                barrier_pending_digest=None,
                barrier_entered_at=None,
            )
        )

    def enter_barrier(self, pending_digest: str, *, now: str) -> AuthorityBarrier:
        """Typed barrier: old owners stay on their frozen authority; fresh refused."""
        state = self.state()
        self._write_state(
            replace(
                state,
                barrier_pending_digest=pending_digest,
                barrier_entered_at=now,
            )
        )
        census = self.census(state.active_digest) if state.active_digest is not None else 0
        return AuthorityBarrier(
            pending_digest=pending_digest,
            active_digest=state.active_digest,
            live_owner_census=census,
        )

    # -- owners --------------------------------------------------------------

    def register_owner(self, owner: DaggerOwner) -> None:
        records = self.owners()
        if any(record.get("ownerId") == owner.owner_id for record in records):
            raise DaggerRuntimeAuthorityError(
                "runtime authority owner is already registered",
                (
                    _finding(
                        "runtime-authority-owner-conflict",
                        f"registry.owners.{owner.owner_id}",
                        "one live operation already owns this authority identity",
                    ),
                ),
            )
        self._write_owners([*records, _owner_record(owner)])

    def release_owner(self, owner: DaggerOwner) -> None:
        """Release only the exact owner; liveness evidence must still match."""
        records = self.owners()
        matches = [record for record in records if record.get("ownerId") == owner.owner_id]
        if not matches:
            raise DaggerRuntimeAuthorityError(
                "runtime authority owner is not registered",
                (
                    _finding(
                        "runtime-authority-no-owner",
                        f"registry.owners.{owner.owner_id}",
                        "terminalization can only release its exact registered owner",
                    ),
                ),
            )
        record = matches[0]
        if record.get("processFingerprint") != owner.process_fingerprint or not _record_live(
            record, None
        ):
            raise DaggerRuntimeAuthorityError(
                "runtime authority owner liveness is ambiguous or stale",
                (
                    _finding(
                        "runtime-authority-stale-owner",
                        f"registry.owners.{owner.owner_id}",
                        "the releasing process is not the live process that registered the owner",
                    ),
                ),
            )
        self._write_owners(
            [record for record in records if record.get("ownerId") != owner.owner_id]
        )


def _owner_record(owner: DaggerOwner) -> dict[str, object]:
    return {
        "ownerId": owner.owner_id,
        "authorityDigest": owner.authority_digest,
        "operationKind": owner.operation_kind,
        "scope": owner.scope,
        "generation": owner.generation,
        "pid": owner.pid,
        "processFingerprint": owner.process_fingerprint,
        "startedAt": owner.started_at,
    }


def _record_live(
    record: dict[str, object], liveness: Callable[[dict[str, object]], bool] | None
) -> bool:
    if liveness is not None:
        return liveness(record)
    fingerprint = record.get("processFingerprint")
    pid = record.get("pid")
    if not isinstance(fingerprint, str) or not isinstance(pid, int):
        return False
    return process_fingerprint_live(pid, fingerprint)


def process_fingerprint_live(pid: int, fingerprint: str) -> bool:
    """Prove one owner PID still belongs to the same process-start instance."""
    current = _host_process_fingerprint(pid)
    return current is not None and current == fingerprint


def _host_process_fingerprint(pid: int) -> str | None:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        end = stat_text.rfind(")")
        fields = stat_text[end + 2 :].split()
        start_ticks = fields[19]
    except (IndexError, OSError):
        return None
    if not boot_id:
        return None
    return _sha256(f"{boot_id}\0{pid}\0{start_ticks}")


def current_process_fingerprint() -> str:
    """Fingerprint for this process; PID-reuse-safe owner identity input."""
    fingerprint = _host_process_fingerprint(os.getpid())
    if fingerprint is None:
        fingerprint = _sha256(f"non-linux\0{os.getpid()}\0{time.time_ns()}")
    return fingerprint


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest_or_error(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise DaggerRuntimeAuthorityError(
            "runtime authority registry state is corrupt",
            (
                _finding(
                    "runtime-authority-registry-corrupt",
                    f"registry.{path}",
                    "authority digest cell is invalid",
                ),
            ),
        )
    return value


def owner_identity(
    *,
    operation_kind: str,
    scope: str,
    generation: str,
    authority_digest: str,
) -> str:
    return _sha256(
        {
            "operationKind": operation_kind,
            "scope": scope,
            "generation": generation,
            "authorityDigest": authority_digest,
        }
    )


def default_registry_root(environ: Mapping[str, str] | None = None) -> Path:
    resolved = os.environ if environ is None else environ
    configured = resolved.get(HOST_REGISTRY_ROOT_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".agents-remember" / "dagger-authority"


def admit_dagger_authority(
    *,
    environ: Mapping[str, str] | None = None,
    inspector: EngineInspectionProtocol | None = None,
    registry: AuthorityRegistry | None = None,
    owner_factory: Callable[[DaggerAuthoritySnapshot], DaggerOwner] | None = None,
) -> AdmittedDaggerAuthority:
    """Admit one authority before any Dagger command starts.

    Refuses missing/malformed/unsupported/provisioning-capable declarations, a
    non-running engine or missing store mount, ambient conflicts, and an active
    transition barrier with live owners -- all with typed defects. Freezes one
    immutable snapshot and (when owner_factory is supplied) registers the live
    consumer in the host-level registry. A path-based host declaration is separately
    refused at :func: when it lives inside a repository.
    """
    checked = load_host_declaration(environ)
    resolved_inspector = inspector or DockerEngineInspector()
    inspected = resolved_inspector.inspect(checked)
    if not inspected.engine_running:
        _raise_authority(
            "runtime-authority-inspection-failed",
            "engine",
            f"engine {inspected.engine_id!r} is not running",
        )
    if inspected.store_mounted_path != checked.layer_store:
        _raise_authority(
            "runtime-authority-inspection-failed",
            "layer-store",
            (
                f"engine mounts {inspected.store_mounted_path!r}, not the declared "
                f"{checked.layer_store!r}"
            ),
        )
    snapshot = authority_snapshot(checked, inspected, inspected_at=_now())
    env = os.environ if environ is None else environ
    launch_env = authority_environment(snapshot, base=dict(env))
    if registry is None:
        return AdmittedDaggerAuthority(
            snapshot=snapshot,
            declaration=checked,
            environment=launch_env,
            owner=None,
        )
    registered_owner = _admit_with_registry(
        registry,
        snapshot,
        owner_factory=owner_factory,
    )
    return AdmittedDaggerAuthority(
        snapshot=snapshot,
        declaration=checked,
        environment=launch_env,
        owner=registered_owner,
    )


def _admit_with_registry(
    registry: AuthorityRegistry,
    snapshot: DaggerAuthoritySnapshot,
    *,
    owner_factory: Callable[[DaggerAuthoritySnapshot], DaggerOwner] | None,
) -> DaggerOwner | None:
    with registry.exclusive_access():
        state = registry.state()
        if state.barrier_pending_digest is not None:
            census = registry.census(state.active_digest) if state.active_digest is not None else 0
            if census > 0 or state.barrier_pending_digest != snapshot.snapshot_digest:
                _raise_transition_barrier(
                    pending=state.barrier_pending_digest,
                    active=state.active_digest,
                    census=census,
                    requested=snapshot.snapshot_digest,
                )
            registry.activate(snapshot.snapshot_digest, now=_now())
        elif state.active_digest != snapshot.snapshot_digest:
            census = registry.census(state.active_digest) if state.active_digest is not None else 0
            if census > 0:
                barrier = registry.enter_barrier(snapshot.snapshot_digest, now=_now())
                _raise_transition_barrier(
                    pending=barrier.pending_digest,
                    active=barrier.active_digest,
                    census=barrier.live_owner_census,
                    requested=snapshot.snapshot_digest,
                )
            registry.activate(snapshot.snapshot_digest, now=_now())
        resolved_owner = owner_factory(snapshot) if owner_factory is not None else None
        if resolved_owner is not None:
            registry.register_owner(resolved_owner)
    return resolved_owner


def _raise_transition_barrier(
    *,
    pending: str,
    active: str | None,
    census: int,
    requested: str,
) -> Never:
    raise DaggerRuntimeAuthorityError(
        "runtime authority changed while live owners still use the old authority",
        (
            _finding(
                "runtime-authority-transition-barrier",
                "registry.authority",
                (
                    "the host declaration changed; existing owners finish on their frozen "
                    "authority, and every fresh admission is refused until the old "
                    "authority's owner census reaches zero"
                ),
                extra={
                    "pendingDigest": pending,
                    "activeDigest": active if active is not None else "",
                    "requestedDigest": requested,
                    "liveOwnerCensus": census,
                },
            ),
        ),
    )


def reuse_authority_for_continuation(
    snapshot: DaggerAuthoritySnapshot,
    *,
    registry: AuthorityRegistry,
    owner: DaggerOwner | None = None,
) -> DaggerOwner | None:
    """Retry/recovery/lifecycle continuation on the frozen authority snapshot.

    Reuses the original admitted snapshot; it never re-resolves ambient state and never
    silently binds a different engine or store. A changed host declaration with live
    owners on another digest stays behind the typed transition barrier.
    """
    with registry.exclusive_access():
        state = registry.state()
        if state.active_digest == snapshot.snapshot_digest:
            # The original frozen authority is still the active one: a retry/recovery
            # continuation is admitted on it exactly like the owners already on it.
            pass
        elif state.barrier_pending_digest is not None:
            census = registry.census(state.active_digest) if state.active_digest is not None else 0
            if state.barrier_pending_digest == snapshot.snapshot_digest and census == 0:
                registry.activate(snapshot.snapshot_digest, now=_now())
            else:
                _raise_transition_barrier(
                    pending=state.barrier_pending_digest,
                    active=state.active_digest,
                    census=census,
                    requested=snapshot.snapshot_digest,
                )
        elif state.active_digest is not None:
            census = registry.census(state.active_digest)
            if census > 0:
                barrier = registry.enter_barrier(snapshot.snapshot_digest, now=_now())
                _raise_transition_barrier(
                    pending=barrier.pending_digest,
                    active=barrier.active_digest,
                    census=barrier.live_owner_census,
                    requested=snapshot.snapshot_digest,
                )
            registry.activate(snapshot.snapshot_digest, now=_now())
        else:
            registry.activate(snapshot.snapshot_digest, now=_now())
        if owner is not None:
            records = registry.owners()
            if not any(record.get("ownerId") == owner.owner_id for record in records):
                registry.register_owner(owner)
    return owner


def release_dagger_authority(
    admitted: AdmittedDaggerAuthority,
    *,
    registry: AuthorityRegistry,
) -> None:
    """Terminalize one owner; release only the exact owner record."""
    if admitted.owner is None:
        return
    with registry.exclusive_access():
        registry.release_owner(admitted.owner)


def _finding(
    code: str,
    path: str,
    detail: str,
    *,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"code": code, "path": path, "detail": detail}
    if extra:
        payload.update(extra)
    return payload


def _raise_authority(code: str, path: str, detail: str) -> Never:
    raise DaggerRuntimeAuthorityError(
        f"shared Dagger runtime authority refused: {detail}",
        (_finding(code, path, detail),),
    )


__all__ = [
    "DAGGER_HOST_ENV",
    "HOST_DECLARATION_ENV",
    "HOST_REGISTRY_ROOT_ENV",
    "RUNTIME_AUTHORITY_DIGEST_ENV",
    "AdmittedDaggerAuthority",
    "AuthorityBarrier",
    "AuthorityRegistry",
    "AuthorityState",
    "DaggerAuthoritySnapshot",
    "DaggerHostDeclaration",
    "DaggerOwner",
    "DockerEngineInspector",
    "EngineInspectionProtocol",
    "InspectedRuntime",
    "admit_dagger_authority",
    "authority_environment",
    "authority_snapshot",
    "current_process_fingerprint",
    "default_registry_root",
    "load_host_declaration",
    "owner_identity",
    "parse_host_declaration",
    "process_fingerprint_live",
    "release_dagger_authority",
    "reuse_authority_for_continuation",
]
