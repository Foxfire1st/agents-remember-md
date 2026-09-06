"""Host registry locking composes with undeclared checkout coordination isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from agents_remember.controlplane import durable_store
from agents_remember.errors import DaggerRuntimeAuthorityError, LockCapabilityError
from agents_remember.kernel import file_lock
from agents_remember.kernel.primitives import checkout_coordination
from agents_remember.worktrees.modules.quality import dagger_authority as authority


@dataclass(frozen=True)
class _Inspector:
    engine: str

    def inspect(self, declaration: authority.DaggerHostDeclaration) -> authority.InspectedRuntime:
        return authority.InspectedRuntime(
            engine_id=self.engine,
            engine_running=True,
            store_mounted_path=declaration.layer_store,
            store_source="/var/lib/docker/volumes/shared-layers/_data",
            observed_version="dagger 0.21.8",
        )


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> authority.AuthorityRegistry:
    checkout = tmp_path / "enclosure" / "candidate"
    source = checkout / "mcp" / "src" / "agents_remember" / "kernel" / "file_lock.py"
    source.parent.mkdir(parents=True)
    source.touch()
    (checkout / "mcp" / "pyproject.toml").touch()
    (checkout / ".git").write_text("gitdir: /synthetic-linked-checkout\n", encoding="utf-8")
    monkeypatch.setattr(checkout_coordination, "_PACKAGE_SOURCE", source)
    monkeypatch.delitem(checkout_coordination._declared, "mode", raising=False)
    assert checkout_coordination.declared_execution_mode() is None
    location = checkout_coordination.resolve_checkout_location()
    assert location is not None and location.kind == "linked"
    return authority.AuthorityRegistry(tmp_path / "host-authority")


def _admit(
    registry: authority.AuthorityRegistry,
    *,
    engine: str = "shared-engine",
    scope: str = "quality",
) -> authority.AdmittedDaggerAuthority:
    declaration = authority.DaggerHostDeclaration(
        schema_version=authority.AUTHORITY_SCHEMA_VERSION,
        endpoint=f"container://{engine}",
        layer_store=authority.DEFAULT_LAYER_STORE,
        engine_version=authority.DEFAULT_ENGINE_VERSION,
        source=f"env:{authority.HOST_DECLARATION_ENV}",
    )

    def owner(snapshot: authority.DaggerAuthoritySnapshot) -> authority.DaggerOwner:
        return authority.DaggerOwner(
            owner_id=authority.owner_identity(
                operation_kind="quality",
                scope=scope,
                generation="generation-1",
                authority_digest=snapshot.snapshot_digest,
            ),
            authority_digest=snapshot.snapshot_digest,
            operation_kind="quality",
            scope=scope,
            generation="generation-1",
            pid=os.getpid(),
            process_fingerprint=authority.current_process_fingerprint(),
            started_at="2026-09-05T00:00:00+00:00",
        )

    return authority.admit_dagger_authority(
        environ={authority.HOST_DECLARATION_ENV: json.dumps(declaration.as_manifest())},
        inspector=_Inspector(engine),
        registry=registry,
        owner_factory=owner,
    )


def test_host_admission_keeps_undeclared_checkout_coordinator_writes_refused(
    registry: authority.AuthorityRegistry, tmp_path: Path
) -> None:
    live_log = tmp_path / "live-coordinator" / "observer" / "operator-inbox.jsonl"
    with (
        pytest.raises(checkout_coordination.CheckoutCoordinationError),
        durable_store.exclusive_access(live_log, durable_store.OPERATOR_INBOX_OWNERSHIP),
    ):
        pytest.fail("live coordinator lock must refuse before entering")
    assert not live_log.parent.exists()

    admitted = _admit(registry)
    assert admitted.owner is not None
    assert registry.census(admitted.snapshot_digest) == 1
    assert registry.root.joinpath("authority.lock.lock").is_file()
    assert not registry.lock_path.exists()
    authority.reuse_authority_for_continuation(
        admitted.snapshot, registry=registry, owner=admitted.owner
    )
    assert registry.census(admitted.snapshot_digest) == 1
    assert checkout_coordination.declared_execution_mode() is None
    with pytest.raises(checkout_coordination.CheckoutCoordinationError):
        durable_store.append_line(live_log, "forbidden")
    assert not live_log.parent.exists()
    authority.release_dagger_authority(admitted, registry=registry)
    assert registry.owners() == []


def test_undeclared_registry_preserves_transition_barrier_and_exact_owner_release(
    registry: authority.AuthorityRegistry,
) -> None:
    first = _admit(registry)
    sibling = _admit(registry, scope="integration")
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        _admit(registry, engine="replacement-engine")
    assert caught.value.findings[0]["code"] == "runtime-authority-transition-barrier"
    assert registry.state().active_digest == first.snapshot_digest
    authority.reuse_authority_for_continuation(first.snapshot, registry=registry, owner=first.owner)
    authority.release_dagger_authority(first, registry=registry)
    assert registry.census(first.snapshot_digest) == 1
    with pytest.raises(DaggerRuntimeAuthorityError):
        _admit(registry, engine="replacement-engine")
    authority.release_dagger_authority(sibling, registry=registry)
    replacement = _admit(registry, engine="replacement-engine")
    assert replacement.snapshot_digest != first.snapshot_digest
    assert registry.state().barrier_pending_digest is None
    authority.release_dagger_authority(replacement, registry=registry)
    assert registry.owners() == []


def _process_lock_status(path: Path) -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import fcntl, sys\n"
            "with open(sys.argv[1], 'a+b') as handle:\n"
            "    try:\n"
            "        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError:\n"
            "        sys.exit(17)\n",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.stderr == ""
    return result.returncode


def _thread_can_take_mutex(path: Path) -> bool:
    mutex = file_lock.thread_mutex_for(path)
    acquired = mutex.acquire(blocking=False)
    if acquired:
        mutex.release()
    return acquired


def test_registry_nested_exception_retains_then_releases_thread_and_process_exclusion(
    registry: authority.AuthorityRegistry,
) -> None:
    physical_lock = registry.root / "authority.lock.lock"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError, match="outer body"), registry.exclusive_access():
            assert not pool.submit(_thread_can_take_mutex, registry.lock_path).result(timeout=10)
            assert _process_lock_status(physical_lock) == 17
            with pytest.raises(ValueError, match="nested body"), registry.exclusive_access():
                raise ValueError("nested body")
            assert file_lock.lock_held(registry.lock_path)
            assert _process_lock_status(physical_lock) == 17
            raise ValueError("outer body")
        assert not file_lock.lock_held(registry.lock_path)
        assert pool.submit(_thread_can_take_mutex, registry.lock_path).result(timeout=10)
        assert _process_lock_status(physical_lock) == 0
        with registry.exclusive_access():
            assert _process_lock_status(physical_lock) == 17


def test_registry_refuses_ineffective_flock_before_state_and_can_recover(
    registry: authority.AuthorityRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(file_lock.fcntl, "flock", lambda _fd, _operation: None)
        with pytest.raises(DaggerRuntimeAuthorityError) as caught:
            _admit(registry)
    assert caught.value.findings[0]["code"] == "runtime-authority-registry-lock-unsafe"
    assert isinstance(caught.value.__cause__, LockCapabilityError)
    assert not registry.state_path.exists()
    assert not registry.owners_path.exists()
    assert not file_lock.lock_held(registry.lock_path)
    admitted = _admit(registry)
    assert registry.census(admitted.snapshot_digest) == 1
    authority.release_dagger_authority(admitted, registry=registry)
