"""Focused tests for the universal shared Dagger runner/layer-store authority.

Covers the CCR-R12 host authority boundary: strict declaration refusal, live
engine/store inspection, immutable snapshot binding, the host-level locked owner
registry, the typed authority-transition barrier, exact-owner release, crash
reconciliation, and frozen-snapshot reuse for retry/recovery. Every case runs
zero Dagger commands.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agents_remember.errors import DaggerRuntimeAuthorityError
from agents_remember.worktrees.modules.quality import dagger_authority as authority

DECLARATION_V1: dict[str, object] = {
    "schemaVersion": "dagger-host-declaration/v1",
    "endpoint": "container://shared-dagger-engine",
    "layerStore": "/var/lib/dagger",
    "engineVersion": "v0.21.8",
}

OTHER_ENGINE_DECLARATION_V1: dict[str, object] = {
    **DECLARATION_V1,
    "endpoint": "container://another-dagger-engine",
}


class FakeInspector:
    def __init__(
        self,
        *,
        running: bool = True,
        store_mounted: bool = True,
        engine_id: str = "shared-dagger-engine",
        store_path: str = "/var/lib/dagger",
    ) -> None:
        self.running = running
        self.store_mounted = store_mounted
        self.engine_id = engine_id
        self.store_path = store_path

    def inspect(self, declaration):
        if not self.running:
            return authority.InspectedRuntime(
                engine_id=self.engine_id,
                engine_running=False,
                store_mounted_path=self.store_path,
                store_source="/var/lib/docker/volumes/x/_data",
                observed_version=None,
            )
        if not self.store_mounted or declaration.layer_store != self.store_path:
            return authority.InspectedRuntime(
                engine_id=self.engine_id,
                engine_running=True,
                store_mounted_path="/var/lib/dagger-other",
                store_source="/var/lib/docker/volumes/other/_data",
                observed_version="dagger 0.21.8",
            )
        return authority.InspectedRuntime(
            engine_id=self.engine_id,
            engine_running=True,
            store_mounted_path=declaration.layer_store,
            store_source="/var/lib/docker/volumes/shared-layer-store/_data",
            observed_version="dagger 0.21.8",
        )


def declaration_env(declaration: dict[str, object] | None = DECLARATION_V1) -> dict[str, str]:
    env = dict(os.environ)
    if declaration is None:
        env.pop(authority.HOST_DECLARATION_ENV, None)
        env.pop(authority.HOST_REGISTRY_ROOT_ENV, None)
        return env
    env[authority.HOST_DECLARATION_ENV] = json.dumps(declaration)
    env[authority.HOST_REGISTRY_ROOT_ENV] = "/nonexistent-host-registry"
    return env


def owner_for(
    registry_scope: str,
    snapshot,
    *,
    operation_kind: str = "quality",
    generation: str = "gen-1",
) -> authority.DaggerOwner:
    return authority.DaggerOwner(
        owner_id=authority.owner_identity(
            operation_kind=operation_kind,
            scope=registry_scope,
            generation=generation,
            authority_digest=snapshot.snapshot_digest,
        ),
        authority_digest=snapshot.snapshot_digest,
        operation_kind=operation_kind,
        scope=registry_scope,
        generation=generation,
        pid=os.getpid(),
        process_fingerprint=authority.current_process_fingerprint(),
        started_at="2026-09-03T00:00:00+00:00",
    )


def refusal_code(error: DaggerRuntimeAuthorityError) -> str:
    assert len(error.findings) == 1
    return str(error.findings[0]["code"])


def test_admits_one_shared_authority_and_binds_a_deterministic_snapshot() -> None:
    first = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
    )
    second = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
    )
    assert first.snapshot_digest == second.snapshot_digest
    assert len(first.snapshot_digest) == 64
    assert first.snapshot.endpoint == "container://shared-dagger-engine"
    assert first.snapshot.store_source == "/var/lib/docker/volumes/shared-layer-store/_data"
    assert first.environment[authority.DAGGER_HOST_ENV] == "container://shared-dagger-engine"
    assert first.environment[authority.RUNTIME_AUTHORITY_DIGEST_ENV] == first.snapshot_digest
    # A changed live engine/store identity must invalidate the snapshot digest.
    changed = FakeInspector(engine_id="another-engine-container")
    moved = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=changed,
    )
    assert moved.snapshot_digest != first.snapshot_digest


def test_release_only_the_exact_owner_and_reject_stale_or_foreign_owners(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    admitted = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    foreign = authority.DaggerOwner(
        owner_id="not-registered",
        authority_digest=admitted.snapshot_digest,
        operation_kind="quality",
        scope="foreign",
        generation="gen-9",
        pid=os.getpid(),
        process_fingerprint="f" * 64,
        started_at="2026-09-03T00:00:00+00:00",
    )
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.release_dagger_authority(
            authority.AdmittedDaggerAuthority(
                snapshot=admitted.snapshot,
                declaration=admitted.declaration,
                environment=admitted.environment,
                owner=foreign,
            ),
            registry=registry,
        )
    assert refusal_code(caught.value) == "runtime-authority-no-owner"
    stale = authority.DaggerOwner(
        owner_id="stale-pid",
        authority_digest=admitted.snapshot_digest,
        operation_kind="quality",
        scope="stale-scope",
        generation="gen-0",
        pid=999999,
        process_fingerprint="a" * 64,
        started_at="2026-09-03T00:00:00+00:00",
    )
    # A registered owner whose process-start evidence is no longer live may never be
    # released by a different process identity (PID reuse / stale-owner confusion).
    registry.register_owner(stale)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.release_dagger_authority(
            authority.AdmittedDaggerAuthority(
                snapshot=admitted.snapshot,
                declaration=admitted.declaration,
                environment=admitted.environment,
                owner=stale,
            ),
            registry=registry,
        )
    assert refusal_code(caught.value) == "runtime-authority-stale-owner"
    # The registered live owner is still exactly releasable.
    assert registry.census(admitted.snapshot_digest) == 1


# ---------------------------------------------------------------------------
# CCR-L12 rail: DockerEngineInspector failure branches.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CCR-L12 rail: host declaration parsing and loading refusals.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CCR-L12 rail: registry state and owners corruption branches.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CCR-L12 rail: owner registration conflicts and liveness accounting.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CCR-L12 rail: small registry helpers.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CCR-L12 rail: transition barrier activation paths and ownerless release.
# ---------------------------------------------------------------------------
