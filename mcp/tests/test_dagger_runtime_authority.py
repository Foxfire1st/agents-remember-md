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
import subprocess
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


def test_refuses_a_missing_host_declaration_before_any_engine_contact() -> None:
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.admit_dagger_authority(
            environ=declaration_env(None),
            inspector=FakeInspector(),
        )
    assert refusal_code(caught.value) == "runtime-authority-missing"


def test_refuses_malformed_host_declaration() -> None:
    env = declaration_env(None)
    env[authority.HOST_DECLARATION_ENV] = "{not-json"
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.admit_dagger_authority(environ=env, inspector=FakeInspector())
    assert refusal_code(caught.value) == "runtime-authority-malformed"


def test_refuses_unsupported_and_provisioning_capable_endpoints() -> None:
    for endpoint, expected in (
        ("ftp://engine", "runtime-authority-unsupported"),
        ("docker-container://engine", "runtime-authority-provisioning-capable"),
        ("docker-image://engine:tag", "runtime-authority-provisioning-capable"),
        ("local", "runtime-authority-provisioning-capable"),
        ("just-an-engine-name", "runtime-authority-provisioning-capable"),
    ):
        with pytest.raises(DaggerRuntimeAuthorityError) as caught:
            authority.parse_host_declaration(
                {**DECLARATION_V1, "endpoint": endpoint},
                source="env:AR_DAGGER_RUNTIME_AUTHORITY",
            )
        assert refusal_code(caught.value) == expected, endpoint


def test_refuses_a_worktree_local_declaration_source() -> None:
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.parse_host_declaration(
            DECLARATION_V1,
            source="/home/user/projects/repo/worktrees/leaf/config/dagger-authority.json",
            excluded_roots=(Path("/home/user/projects/repo"),),
        )
    assert refusal_code(caught.value) == "runtime-authority-worktree-local"


def test_refuses_an_engine_that_is_not_running_and_a_missing_store_mount() -> None:
    stopped = authority.admit_dagger_authority is None
    del stopped
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.admit_dagger_authority(
            environ=declaration_env(),
            inspector=FakeInspector(running=False),
        )
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"

    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.admit_dagger_authority(
            environ=declaration_env(),
            inspector=FakeInspector(store_mounted=False),
        )
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"


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


def test_refuses_an_ambient_dagger_host_conflict_with_the_admitted_authority() -> None:
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.admit_dagger_authority(
            environ={**declaration_env(), authority.DAGGER_HOST_ENV: "container://rogue-engine"},
            inspector=FakeInspector(),
        )
    assert refusal_code(caught.value) == "runtime-authority-ambient-conflict"


def test_two_worktrees_and_two_operation_kinds_reuse_one_authority_identity(
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
    admitted_b = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-b/worktree-2", snapshot, operation_kind="integration"
        ),
    )
    assert admitted.snapshot_digest == admitted_b.snapshot_digest
    assert registry.census(admitted.snapshot_digest) == 2
    assert len(registry.live_owners()) == 2
    assert registry.state().active_digest == admitted.snapshot_digest
    authority.release_dagger_authority(admitted, registry=registry)
    assert registry.census(admitted.snapshot_digest) == 1
    # Ordinary release retains the shared runner while another owner is live.
    assert registry.state().active_digest == admitted.snapshot_digest


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


def test_changed_host_declaration_enters_a_typed_barrier_and_activates_after_zero_census(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    old = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    new_declaration = {**DECLARATION_V1, "endpoint": "container://replacement-engine"}
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.admit_dagger_authority(
            environ=declaration_env(new_declaration),
            inspector=FakeInspector(engine_id="replacement-engine"),
            registry=registry,
            owner_factory=lambda snapshot: owner_for(
                "repo-c/worktree-3", snapshot, operation_kind="closeout"
            ),
        )
    error = caught.value
    assert refusal_code(error) == "runtime-authority-transition-barrier"
    finding = error.findings[0]
    assert finding["pendingDigest"] != finding["activeDigest"]
    assert finding["activeDigest"] == old.snapshot_digest
    assert finding["liveOwnerCensus"] == 1
    assert finding["requestedDigest"] != old.snapshot_digest
    # The old operation still finishes on its frozen authority.
    old_continuation = authority.reuse_authority_for_continuation(
        old.snapshot,
        registry=registry,
        owner=old.owner,
    )
    assert old_continuation is not None
    # Atomic activation happens only after the locked zero-owner census.
    authority.release_dagger_authority(old, registry=registry)
    replaced = authority.admit_dagger_authority(
        environ=declaration_env(new_declaration),
        inspector=FakeInspector(engine_id="replacement-engine"),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-c/worktree-3", snapshot, operation_kind="closeout"
        ),
    )
    assert replaced.snapshot_digest != old.snapshot_digest
    assert registry.state().active_digest == replaced.snapshot_digest
    assert registry.state().barrier_pending_digest is None
    assert registry.census(replaced.snapshot_digest) == 1


def test_continuation_reuses_the_frozen_snapshot_and_never_resolves_ambient_state(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    original = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    # The host declaration changes while the operation is live.
    new_declaration = {**DECLARATION_V1, "endpoint": "container://replacement-engine"}
    changed_env = declaration_env(new_declaration)
    with pytest.raises(DaggerRuntimeAuthorityError):
        authority.admit_dagger_authority(
            environ=changed_env,
            inspector=FakeInspector(engine_id="replacement-engine"),
            registry=registry,
            owner_factory=lambda snapshot: owner_for(
                "repo-d/worktree-4", snapshot, operation_kind="quality"
            ),
        )
    # Retry/recovery reuse the original admitted snapshot, not the changed ambient state.
    retry_owner = authority.reuse_authority_for_continuation(
        original.snapshot,
        registry=registry,
        owner=authority.DaggerOwner(
            owner_id="retry-owner",
            authority_digest=original.snapshot_digest,
            operation_kind="quality",
            scope="repo-a/worktree-1",
            generation="gen-1-retry",
            pid=os.getpid(),
            process_fingerprint=authority.current_process_fingerprint(),
            started_at="2026-09-03T00:01:00+00:00",
        ),
    )
    assert retry_owner is not None
    assert registry.census(original.snapshot_digest) == 2
    # A continuation on a different snapshot stays refused while old owners exist.
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.reuse_authority_for_continuation(
            authority.admit_dagger_authority(
                environ=changed_env,
                inspector=FakeInspector(engine_id="replacement-engine"),
            ).snapshot,
            registry=registry,
        )
    assert refusal_code(caught.value) == "runtime-authority-transition-barrier"


def test_crash_reconciliation_drops_stale_owners_without_touching_live_results(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    live = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    stale_record = {
        "ownerId": "crashed-worker",
        "authorityDigest": live.snapshot_digest,
        "operationKind": "closeout",
        "scope": "repo-e/worktree-5",
        "generation": "gen-crashed",
        "pid": 999999,
        "processFingerprint": "c" * 64,
        "startedAt": "2026-09-03T00:00:00+00:00",
    }
    records = registry.owners()
    registry._write_owners([*records, stale_record])  # pyright: ignore[reportPrivateUsage]
    # The census reconciles stale ownership (crash recovery) and counts only the live owner.
    assert registry.census(live.snapshot_digest) == 1
    assert registry.reconcile_stale() == 0
    assert all(record.get("ownerId") != "crashed-worker" for record in registry.owners())
    assert registry.census(live.snapshot_digest) == 1
    # Primary operation results are untouched: the live owner row survives intact.
    live_owner = live.owner
    assert live_owner is not None
    live_row = next(
        record for record in registry.owners() if record["ownerId"] == live_owner.owner_id
    )
    assert live_row["authorityDigest"] == live.snapshot_digest
    assert live_row["processFingerprint"] == authority.current_process_fingerprint()


def test_registry_corruption_refuses_with_the_typed_defect(tmp_path: Path) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    registry.state()
    registry.state_path.write_text(
        '{"schemaVersion":"dagger-authority-state/v1","active":{}}', encoding="utf-8"
    )
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        registry.state()
    assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"


def _inspector_declaration() -> authority.DaggerHostDeclaration:
    return authority.DaggerHostDeclaration(
        schema_version="dagger-host-declaration/v1",
        endpoint="container://shared-dagger-engine",
        layer_store="/var/lib/dagger",
        engine_version="v0.21.8",
        source="env:AR_DAGGER_RUNTIME_AUTHORITY",
    )


def test_docker_inspector_proves_the_running_engine_and_store(monkeypatch) -> None:
    """The real inspector resolves docker state, the mount, and the CLI version."""

    declaration = _inspector_declaration()
    mounts = json.dumps(
        [{"Destination": "/var/lib/dagger", "Source": "/var/lib/docker/volumes/x/_data"}]
    )
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="true", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=mounts, stderr=""),
            subprocess.CompletedProcess([], 0, stdout="dagger 0.21.8", stderr=""),
        ]
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert _kwargs.get("stdin") == subprocess.DEVNULL
        return next(results)

    monkeypatch.setattr(authority.subprocess, "run", fake_run)
    inspected = authority.DockerEngineInspector().inspect(declaration)
    assert inspected.engine_running is True
    assert inspected.engine_id == "shared-dagger-engine"
    assert inspected.store_mounted_path == "/var/lib/dagger"
    assert inspected.store_source == "/var/lib/docker/volumes/x/_data"
    assert inspected.observed_version == "dagger 0.21.8"
    # The immutable snapshot manifest exposes the exact admitted engine/store facts.
    snapshot = authority.authority_snapshot(
        declaration, inspected, inspected_at="2026-09-04T00:00:00Z"
    )
    manifest = snapshot.as_manifest()
    assert manifest["snapshotDigest"] == snapshot.snapshot_digest
    assert manifest["engineId"] == "shared-dagger-engine"
    assert declaration.as_manifest()["layerStore"] == "/var/lib/dagger"
    assert len(declaration.declaration_digest) == 64


def test_docker_inspector_refuses_missing_or_stopped_or_unmounted_engines(monkeypatch) -> None:
    """Every docker refusal maps to the typed runtime-authority defect."""

    declaration = _inspector_declaration()

    def refusal_codes() -> list[str]:
        raised = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if "{{.State.Running}}" in command:
                raised.append("running-call")
                return subprocess.CompletedProcess([], 1, stdout="", stderr="docker error")
            raise AssertionError(command)

        monkeypatch.setattr(authority.subprocess, "run", fake_run)
        with pytest.raises(DaggerRuntimeAuthorityError):
            authority.DockerEngineInspector().inspect(declaration)
        return [raised[0]]

    # Missing container: docker inspect fails.
    assert refusal_codes() == ["running-call"]

    # Existing but stopped container.
    def stopped(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="false", stderr="")

    monkeypatch.setattr(authority.subprocess, "run", stopped)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.DockerEngineInspector().inspect(declaration)
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"

    # Running engine without the declared store mount.
    seq = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="true", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="[]", stderr=""),
        ]
    )

    def missing_mount(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return next(seq)

    monkeypatch.setattr(authority.subprocess, "run", missing_mount)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.DockerEngineInspector().inspect(declaration)
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"


# ---------------------------------------------------------------------------
# CCR-L12 rail: DockerEngineInspector failure branches.
# ---------------------------------------------------------------------------


def test_docker_inspector_raises_typed_defect_when_running_check_cannot_start(
    monkeypatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("docker executable cannot start")

    monkeypatch.setattr(authority.subprocess, "run", fake_run)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.DockerEngineInspector().inspect(_inspector_declaration())
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"


def test_docker_inspector_raises_typed_defect_when_mount_inspection_fails(
    monkeypatch,
) -> None:
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="true", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="docker error"),
        ]
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return next(results)

    monkeypatch.setattr(authority.subprocess, "run", fake_run)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.DockerEngineInspector().inspect(_inspector_declaration())
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"


@pytest.mark.parametrize(
    "raw_mounts",
    ["{not-json", '{"Destination": "/var/lib/dagger"}'],
)
def test_docker_inspector_refuses_unparsable_or_non_list_mount_output(
    monkeypatch,
    raw_mounts,
) -> None:
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="true", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=raw_mounts, stderr=""),
        ]
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return next(results)

    monkeypatch.setattr(authority.subprocess, "run", fake_run)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.DockerEngineInspector().inspect(_inspector_declaration())
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"


def test_docker_inspector_skips_foreign_and_malformed_mount_entries(
    monkeypatch,
) -> None:
    mounts = json.dumps(
        [
            {"Destination": "/var/lib/dagger-other", "Source": "/src/other"},
            42,
            {"Destination": "/var/lib/dagger", "Source": 5},
        ]
    )
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="true", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=mounts, stderr=""),
        ]
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return next(results)

    monkeypatch.setattr(authority.subprocess, "run", fake_run)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.DockerEngineInspector().inspect(_inspector_declaration())
    assert refusal_code(caught.value) == "runtime-authority-inspection-failed"


def test_docker_inspector_refuses_an_endpoint_that_is_not_one_engine_container() -> None:
    bad = authority.DaggerHostDeclaration(
        schema_version="dagger-host-declaration/v1",
        endpoint="tcp://engine:1234",
        layer_store="/var/lib/dagger",
        engine_version="v0.21.8",
        source="env:AR_DAGGER_RUNTIME_AUTHORITY",
    )
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.DockerEngineInspector().inspect(bad)
    assert refusal_code(caught.value) == "runtime-authority-malformed"


# ---------------------------------------------------------------------------
# CCR-L12 rail: host declaration parsing and loading refusals.
# ---------------------------------------------------------------------------


def test_parse_host_declaration_refuses_missing_non_object_and_unsupported_payloads() -> None:
    source = "env:AR_DAGGER_RUNTIME_AUTHORITY"
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.parse_host_declaration(None, source=source)
    assert refusal_code(caught.value) == "runtime-authority-missing"
    for raw in ([1, 2], {1: "x"}):
        with pytest.raises(DaggerRuntimeAuthorityError) as caught:
            authority.parse_host_declaration(raw, source=source)
        assert refusal_code(caught.value) == "runtime-authority-malformed"
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.parse_host_declaration(
            {**DECLARATION_V1, "schemaVersion": "dagger-host-declaration/v2"},
            source=source,
        )
    assert refusal_code(caught.value) == "runtime-authority-unsupported"


def test_parse_host_declaration_refuses_invalid_endpoint_layer_store_and_engine_version() -> None:
    source = "env:AR_DAGGER_RUNTIME_AUTHORITY"
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.parse_host_declaration(
            {**DECLARATION_V1, "endpoint": "  container://engine  "},
            source=source,
        )
    assert refusal_code(caught.value) == "runtime-authority-malformed"
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.parse_host_declaration(
            {**DECLARATION_V1, "layerStore": "var/lib/dagger"},
            source=source,
        )
    assert refusal_code(caught.value) == "runtime-authority-malformed"
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.parse_host_declaration(
            {**DECLARATION_V1, "engineVersion": "0.21.8"},
            source=source,
        )
    assert refusal_code(caught.value) == "runtime-authority-malformed"


def test_load_host_declaration_refuses_a_non_string_value() -> None:
    non_string: dict[str, object] = {authority.HOST_DECLARATION_ENV: 123}
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.load_host_declaration(non_string)  # pyright: ignore[reportArgumentType]
    assert refusal_code(caught.value) == "runtime-authority-malformed"


class _FalsyRootPath(Path):
    """A Path subclass that is falsy so the empty-root guard branch can run."""

    def __bool__(self) -> bool:
        return False


def test_parse_host_declaration_tolerates_resolve_failures_around_unrelated_roots(
    monkeypatch,
) -> None:
    real_realpath = os.path.realpath

    def guarded_realpath(path: str | bytes, *, strict: bool = False) -> str | bytes:
        rendered = os.fspath(path)
        if isinstance(rendered, bytes):
            if b"authority-resolve-fail" in rendered:
                raise OSError("resolve refused for testing")
        elif "authority-resolve-fail" in rendered:
            raise OSError("resolve refused for testing")
        return real_realpath(rendered, strict=strict)

    monkeypatch.setattr(authority.os.path, "realpath", guarded_realpath)
    parsed = authority.parse_host_declaration(
        DECLARATION_V1,
        source="/etc/authority-resolve-fail/host/dagger-authority.json",
        excluded_roots=(
            _FalsyRootPath("/ignored/root"),
            Path("/etc/authority-resolve-fail/root"),
            Path("/srv/unrelated/repository/root"),
        ),
    )
    assert parsed.endpoint == DECLARATION_V1["endpoint"]


# ---------------------------------------------------------------------------
# CCR-L12 rail: registry state and owners corruption branches.
# ---------------------------------------------------------------------------


def _registry_root(tmp_path: Path, name: str) -> authority.AuthorityRegistry:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return authority.AuthorityRegistry(root)


def test_registry_state_refuses_invalid_json_and_unreadable_state(tmp_path: Path) -> None:
    registry = _registry_root(tmp_path, "state-invalid-json")
    registry.state_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        registry.state()
    assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"

    directory_state = _registry_root(tmp_path, "state-directory")
    directory_state.state_path.mkdir()
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        directory_state.state()
    assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"


def test_registry_state_refuses_non_object_or_wrong_schema_payload(tmp_path: Path) -> None:
    registry = _registry_root(tmp_path, "state-list")
    registry.state_path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        registry.state()
    assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"

    wrong_schema = _registry_root(tmp_path, "state-wrong-schema")
    wrong_schema.state_path.write_text(
        '{"schemaVersion": "dagger-authority-state/v0", "active": null, "barrier": null}',
        encoding="utf-8",
    )
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        wrong_schema.state()
    assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"


def test_registry_state_refuses_non_dict_active_and_malformed_barrier(
    tmp_path: Path,
) -> None:
    registry = _registry_root(tmp_path, "state-bad-active")
    registry.state_path.write_text(
        '{"schemaVersion": "dagger-authority-state/v1", "active": "running", "barrier": null}',
        encoding="utf-8",
    )
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        registry.state()
    assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"

    bad_barrier = _registry_root(tmp_path, "state-bad-barrier")
    bad_barrier.state_path.write_text(
        '{"schemaVersion": "dagger-authority-state/v1", "active": null, '
        '"barrier": {"state": "complete", "pendingDigest": "nope"}}',
        encoding="utf-8",
    )
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        bad_barrier.state()
    assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"


def test_registry_owners_default_when_absent_then_refuse_corrupt_payloads(
    tmp_path: Path,
) -> None:
    fresh = _registry_root(tmp_path, "owners-absent")
    assert fresh.owners() == []

    for name, payload in (
        ("owners-garbage", "{not-json"),
        ("owners-wrong-schema", '{"schemaVersion": "dagger-authority-owners/v0", "owners": []}'),
        (
            "owners-not-list",
            '{"schemaVersion": "dagger-authority-owners/v1", "owners": {"ownerId": "x"}}',
        ),
    ):
        registry = _registry_root(tmp_path, name)
        registry.owners_path.write_text(payload, encoding="utf-8")
        with pytest.raises(DaggerRuntimeAuthorityError) as caught:
            registry.owners()
        assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"


# ---------------------------------------------------------------------------
# CCR-L12 rail: owner registration conflicts and liveness accounting.
# ---------------------------------------------------------------------------


def test_register_owner_twice_refuses_with_owner_conflict(tmp_path: Path) -> None:
    registry = _registry_root(tmp_path, "duplicate-owner")
    snapshot = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
    ).snapshot
    owner = owner_for("repo-z/worktree-9", snapshot, operation_kind="quality")
    registry.register_owner(owner)
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        registry.register_owner(owner)
    assert refusal_code(caught.value) == "runtime-authority-owner-conflict"


def _owner_row(
    owner_id: str,
    digest: str,
    *,
    fingerprint: object,
    pid: object,
) -> dict[str, object]:
    return {
        "ownerId": owner_id,
        "authorityDigest": digest,
        "operationKind": "quality",
        "scope": "scope-" + owner_id,
        "generation": "gen-1",
        "pid": pid,
        "processFingerprint": fingerprint,
        "startedAt": "2026-09-03T00:00:00+00:00",
    }


def test_registry_census_honors_a_custom_liveness_callable(tmp_path: Path) -> None:
    registry = _registry_root(tmp_path, "custom-liveness")
    digest = "ab" * 32
    registry._write_owners(  # pyright: ignore[reportPrivateUsage]
        [
            _owner_row("crashed-worker", digest, fingerprint="c" * 64, pid=999999),
            _owner_row("live-worker", digest, fingerprint="l" * 64, pid=os.getpid()),
        ]
    )
    live = registry.census(digest, liveness=lambda record: record.get("ownerId") == "live-worker")
    assert live == 1
    assert [record["ownerId"] for record in registry.owners()] == ["live-worker"]


def test_registry_census_treats_malformed_owner_cells_as_dead(tmp_path: Path) -> None:
    registry = _registry_root(tmp_path, "malformed-cells")
    digest = "cd" * 32
    registry._write_owners(  # pyright: ignore[reportPrivateUsage]
        [
            _owner_row("fingerprint-worker", digest, fingerprint=123, pid=os.getpid()),
            _owner_row("pid-worker", digest, fingerprint="p" * 64, pid="not-an-int"),
        ]
    )
    assert registry.census(digest) == 0
    assert registry.owners() == []


def test_owner_liveness_matches_the_current_process_and_rejects_dead_pids() -> None:
    fingerprint = authority.current_process_fingerprint()
    assert len(fingerprint) == 64
    assert authority.process_fingerprint_live(os.getpid(), fingerprint) is True
    assert authority.process_fingerprint_live(999999, "f" * 64) is False
    assert (
        authority._host_process_fingerprint(999999) is None  # pyright: ignore[reportPrivateUsage]
    )


class _FakeSystemPath:
    """Stand-in for authority.Path: empty boot id, real /proc reads otherwise."""

    def __init__(self, target: object) -> None:
        self.target = str(target)

    def read_text(self, encoding: str = "utf-8") -> str:
        if "boot_id" in self.target:
            return "\n"
        return Path(self.target).read_text(encoding=encoding)


def test_host_process_fingerprint_returns_none_when_boot_id_is_empty(
    monkeypatch,
) -> None:
    monkeypatch.setattr(authority, "Path", _FakeSystemPath)
    assert (
        authority._host_process_fingerprint(os.getpid()) is None  # pyright: ignore[reportPrivateUsage]
    )


def test_current_process_fingerprint_falls_back_when_host_fingerprint_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(authority, "_host_process_fingerprint", lambda _pid: None)
    fingerprint = authority.current_process_fingerprint()
    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")


# ---------------------------------------------------------------------------
# CCR-L12 rail: small registry helpers.
# ---------------------------------------------------------------------------


def test_digest_or_error_accepts_none_and_refuses_invalid_digest_cells() -> None:
    assert authority._digest_or_error(None, "state.active") is None  # pyright: ignore[reportPrivateUsage]
    for invalid in ("not-a-digest", 12345):
        with pytest.raises(DaggerRuntimeAuthorityError) as caught:
            authority._digest_or_error(  # pyright: ignore[reportPrivateUsage]
                invalid, "state.active"
            )
        assert refusal_code(caught.value) == "runtime-authority-registry-corrupt"


def test_default_registry_root_honors_the_configured_environment() -> None:
    configured = authority.default_registry_root(
        environ={authority.HOST_REGISTRY_ROOT_ENV: "~/dagger-authority-host"}
    )
    assert configured == Path("~/dagger-authority-host").expanduser()


def test_default_registry_root_defaults_under_home_without_configuration() -> None:
    assert authority.default_registry_root(environ={}) == (
        Path.home() / ".agents-remember" / "dagger-authority"
    )


# ---------------------------------------------------------------------------
# CCR-L12 rail: transition barrier activation paths and ownerless release.
# ---------------------------------------------------------------------------


def test_admission_behind_an_existing_barrier_with_live_owners_refuses_again(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    old = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    changed = {**DECLARATION_V1, "endpoint": "container://replacement-engine"}
    for _ in range(2):
        with pytest.raises(DaggerRuntimeAuthorityError) as caught:
            authority.admit_dagger_authority(
                environ=declaration_env(changed),
                inspector=FakeInspector(engine_id="replacement-engine"),
                registry=registry,
                owner_factory=lambda snapshot: owner_for(
                    "repo-c/worktree-3", snapshot, operation_kind="closeout"
                ),
            )
        assert refusal_code(caught.value) == "runtime-authority-transition-barrier"
    assert registry.state().barrier_pending_digest is not None
    assert registry.state().active_digest == old.snapshot_digest


def test_ownerless_admission_and_ownerless_release_leave_the_registry_untouched(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    admitted = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
    )
    assert admitted.owner is None
    assert registry.census(admitted.snapshot_digest) == 0
    authority.release_dagger_authority(admitted, registry=registry)
    assert registry.state().active_digest == admitted.snapshot_digest


def test_continuation_activates_when_the_pending_barrier_matches_and_census_reaches_zero(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    old = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    changed = {**DECLARATION_V1, "endpoint": "container://replacement-engine"}
    with pytest.raises(DaggerRuntimeAuthorityError):
        authority.admit_dagger_authority(
            environ=declaration_env(changed),
            inspector=FakeInspector(engine_id="replacement-engine"),
            registry=registry,
            owner_factory=lambda snapshot: owner_for(
                "repo-c/worktree-3", snapshot, operation_kind="closeout"
            ),
        )
    authority.release_dagger_authority(old, registry=registry)
    replacement = authority.admit_dagger_authority(
        environ=declaration_env(changed),
        inspector=FakeInspector(engine_id="replacement-engine"),
    )
    resumed = authority.reuse_authority_for_continuation(
        replacement.snapshot,
        registry=registry,
    )
    assert resumed is None
    assert registry.state().active_digest == replacement.snapshot_digest
    assert registry.state().barrier_pending_digest is None


def test_continuation_enters_a_barrier_when_the_active_authority_changed_with_live_owners(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    old = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    changed = {**DECLARATION_V1, "endpoint": "container://replacement-engine"}
    replacement = authority.admit_dagger_authority(
        environ=declaration_env(changed),
        inspector=FakeInspector(engine_id="replacement-engine"),
    )
    with pytest.raises(DaggerRuntimeAuthorityError) as caught:
        authority.reuse_authority_for_continuation(
            replacement.snapshot,
            registry=registry,
        )
    assert refusal_code(caught.value) == "runtime-authority-transition-barrier"
    assert registry.state().active_digest == old.snapshot_digest
    assert registry.state().barrier_pending_digest == replacement.snapshot_digest


def test_continuation_activates_a_replacement_once_the_old_census_reaches_zero(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    old = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
        registry=registry,
        owner_factory=lambda snapshot: owner_for(
            "repo-a/worktree-1", snapshot, operation_kind="quality"
        ),
    )
    authority.release_dagger_authority(old, registry=registry)
    changed = {**DECLARATION_V1, "endpoint": "container://replacement-engine"}
    replacement = authority.admit_dagger_authority(
        environ=declaration_env(changed),
        inspector=FakeInspector(engine_id="replacement-engine"),
    )
    resumed = authority.reuse_authority_for_continuation(
        replacement.snapshot,
        registry=registry,
    )
    assert resumed is None
    assert registry.state().active_digest == replacement.snapshot_digest
    assert registry.state().barrier_pending_digest is None


def test_continuation_on_an_empty_registry_activates_the_frozen_snapshot(
    tmp_path: Path,
) -> None:
    registry = authority.AuthorityRegistry(tmp_path)
    admitted = authority.admit_dagger_authority(
        environ=declaration_env(),
        inspector=FakeInspector(),
    )
    resumed = authority.reuse_authority_for_continuation(
        admitted.snapshot,
        registry=registry,
    )
    assert resumed is None
    assert registry.state().active_digest == admitted.snapshot_digest
