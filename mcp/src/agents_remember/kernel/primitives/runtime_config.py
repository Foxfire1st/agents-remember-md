"""Runtime configuration loading for the Agents Remember server.

The runtime configuration record and parser live in kernel, which every layer
may import without reversing the package dependency direction.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from agents_remember.errors import AgentsRememberError
from agents_remember.kernel.agentic_settings import (
    AgenticSettingsError,
    agentic_settings_path,
    load_agentic_settings,
    parse_gate_delegation,
)
from agents_remember.kernel.git_command import run_git
from agents_remember.kernel.primitives import checkout_coordination
from agents_remember.kernel.primitives.gate_policy import DEFAULT_GATE_POLICY, GatePolicy
from agents_remember.kernel.primitives.identity import (
    explicit_provider_instance_id,
    provider_instance_id,
)
from agents_remember.kernel.primitives.provider_degradation_settings import (
    ProviderDegradationSettings,
    ProviderDegradationSettingsError,
    parse_provider_degradation_settings,
)

DEFAULT_PROVIDER_SETUP_SECONDS = 1800
DEFAULT_DOCKER_CONTROL_SECONDS = 120

# The documented timeout caps (see examples/mcp/settings.example.json and
# docs/reference/settings-json.md). Unknown keys are rejected so a typo
# (e.g. "providerSetupSecond") fails loudly instead of being silently stored and
# ignored -- matching how the providers section rejects unknown fields. Only
# providerSetupSeconds is currently consumed by the runtime; toolSeconds is a
# documented, reserved cap.
KNOWN_TIMEOUT_CAPS = frozenset({"providerSetupSeconds", "toolSeconds"})

DEFAULT_DASHBOARD_PORT = 8765

# Same fail-loud discipline as timeoutCaps: a typo ("autostart") must surface at
# boot, not silently leave the daemon unsupervised.
KNOWN_DASHBOARD_FIELDS = frozenset({"autoStart", "port"})
# Completion-seat cleanup is boot-snapshot MCP authority. The two historical edge gates keep
# their names for compatibility; ``autoCloseCompletedSeats`` selects retire (default) versus the
# old landed/archive behavior when an enabled completion edge runs. The legacy autoRetire* names
# remain migration aliases for the edge gates in existing local authority files.
KNOWN_RETIREMENT_FIELDS = frozenset(
    {
        "autoLandOnIntegration",
        "autoLandOnFinalize",
        "autoCloseCompletedSeats",
        "autoRetireOnIntegration",
        "autoRetireOnFinalize",
    }
)
# The agentic orchestration family moved to the coordinator's global settings
# file (260703-L13); the authority file keeps ONLY the one-cycle gateDelegation
# legacy fallback. loops/roles/concurrency/spawn here fail loud, pointing at
# the new home (roles/concurrency were previously reserved-and-silently-dropped
# -- that trap is closed).
KNOWN_AUTHORITY_ORCHESTRATION_FIELDS = frozenset({"gateDelegation"})


class ConfigError(AgentsRememberError):
    """Raised when MCP authority settings are missing or unsafe."""


@dataclass(frozen=True)
class RepositoryScope:
    repo_id: str
    path: Path
    memory_root: Path | None = None
    contract_path: Path | None = None
    certification_profile: Path | None = None


@dataclass(frozen=True)
class ProviderScope:
    provider_id: str
    runtime_root: Path
    log_root: Path
    instance_id: str
    scope: str = "workspace"


@dataclass(frozen=True)
class DashboardSettings:
    """The optional ``dashboard`` settings object (defaults keep it fully off)."""

    auto_start: bool = False
    port: int = DEFAULT_DASHBOARD_PORT


@dataclass(frozen=True)
class OrchestrationSettings:
    """The optional ``orchestration`` settings object."""

    gate_policy: GatePolicy = DEFAULT_GATE_POLICY
    require_reviewer_verdict_at_seams: bool = False


@dataclass(frozen=True)
class RetirementSettings:
    """The optional ``retirement`` settings object: completion-seat cleanup.

    The edge gates and auto-close behavior default ON. A successful completion edge therefore
    retires report-bearing leaf-altitude seats and frees their tmux processes. Turning only
    ``auto_close_completed_seats`` off restores the prior landed/archive behavior end to end.
    """

    auto_land_on_integration: bool = True
    auto_land_on_finalize: bool = True
    auto_close_completed_seats: bool = True


@dataclass(frozen=True)
class McpRuntimeConfig:
    config_path: Path
    coordination_root: Path
    workspace_root: Path
    transcript_root: Path
    harness_skill_root: Path | None = None
    repositories: dict[str, RepositoryScope] = field(default_factory=dict)
    providers: dict[str, ProviderScope] = field(default_factory=dict)
    timeout_caps: dict[str, int] = field(default_factory=dict)
    benchmarks_enabled: bool = False
    # Policy gate for sanctioned direct execution (branch-addressed
    # series-contract bindings and the direct landing operation): explicit
    # opt-ins that refuse unless this is enabled. Fail-closed default.
    direct_execution_enabled: bool = False
    dashboard: DashboardSettings = field(default_factory=DashboardSettings)
    orchestration: OrchestrationSettings = field(default_factory=OrchestrationSettings)
    provider_degradation: ProviderDegradationSettings = field(
        default_factory=ProviderDegradationSettings
    )
    retirement: RetirementSettings = field(default_factory=RetirementSettings)

    @property
    def allowed_repo_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.repositories))

    @property
    def allowed_provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.providers))


def load_config(config_path: str | Path) -> McpRuntimeConfig:
    """Load trusted authority or the leaf-local synthetic checkout config."""
    # Checkout selection precedes reads so a live authority path supplied by unpublished
    # worktree code cannot leak into the candidate writer.
    candidate = _config_for_execution(config_path)
    if isinstance(candidate, McpRuntimeConfig):
        return candidate
    path = candidate
    return config_from_mapping(_read_config_mapping(path), path)


@dataclass(frozen=True)
class ProviderAuthority:
    """The providers map re-read from the on-disk authority settings.

    Containment R1 (260707-HFX-L1): the boot-snapshot config is NOT launch
    authority. A server process loads its config once and closes over it, so
    editing the authority file to ``"providers": {}`` — the operator's only
    fleet-wide kill-switch — previously changed nothing until every running
    server restarted. Launch-capable operations re-read the file through this
    type instead. ``error`` carries the fail-closed reason when the file could
    not be read or parsed: callers must treat that as "no launch authority",
    never fall back to the snapshot. Stopping and cleanup are never gated.
    """

    providers: dict[str, ProviderScope]
    source_path: Path
    error: str | None = None

    def apply(self, config: McpRuntimeConfig) -> McpRuntimeConfig:
        """The boot config with the live providers map swapped in."""
        return replace(config, providers=dict(self.providers))


def reload_provider_authority(config: McpRuntimeConfig) -> ProviderAuthority:
    """Re-read only the providers map from the authority settings file."""
    path = config.config_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return ProviderAuthority(
            providers={},
            source_path=path,
            error=f"cannot re-read MCP authority settings: {path}: {error}",
        )
    if not isinstance(data, dict):
        return ProviderAuthority(
            providers={},
            source_path=path,
            error=f"MCP authority settings must be a JSON object: {path}",
        )
    try:
        providers = parse_providers(
            data.get("providers", {}),
            config.coordination_root,
            config.workspace_root,
        )
    except ConfigError as error:
        return ProviderAuthority(providers={}, source_path=path, error=str(error))
    return ProviderAuthority(providers=providers, source_path=path)


def require_provider_launch_authority(
    config: McpRuntimeConfig, *, operation: str
) -> McpRuntimeConfig:
    """Gate a provider-launching operation on the on-disk authority (fail-closed).

    Returns the boot config with the live providers map applied; raises
    ``ConfigError`` when the authority file disables providers or cannot be
    read. Stop/status/cleanup paths must not call this — stopping is always
    legal.
    """
    authority = reload_provider_authority(config)
    if authority.error is not None:
        raise ConfigError(f"{operation} refused (containment R1, fail-closed): {authority.error}")
    if not authority.providers:
        raise ConfigError(
            f"{operation} refused: providers are disabled in the on-disk authority settings "
            f"({authority.source_path}); the boot snapshot ({sorted(config.providers)}) is not "
            "launch authority (containment R1). stop/status/cleanup remain available."
        )
    return authority.apply(config)


def require_config_path(config_path: str | Path) -> Path:
    if not config_path:
        raise ConfigError("--config is required")

    path = Path(config_path)
    if not path.is_absolute():
        raise ConfigError("--config must be an absolute path")
    path = path.resolve()
    if path.name == "settings.json" and path.parent.name == "system":
        raise ConfigError("coordinator system/settings.json is not an MCP authority settings file")
    if not path.exists():
        raise ConfigError(f"MCP settings file does not exist: {path}")
    if path.is_dir():
        raise ConfigError(f"MCP settings path must be a file: {path}")
    return path


def config_from_mapping(data: dict[str, Any], config_path: Path) -> McpRuntimeConfig:
    coordination_root = required_absolute_path(data, "coordinationRoot")
    workspace_root = required_absolute_path(data, "workspaceRoot")
    transcript_root = optional_absolute_path(data, "transcriptRoot", owner="MCP settings")
    if transcript_root is None:
        transcript_root = coordination_root / "logs" / "mcp"
    harness_skill_root = optional_absolute_path(
        data,
        "harnessSkillRoot",
        owner="MCP settings",
    ) or infer_harness_skill_root(config_path)

    if path_is_relative_to(config_path, coordination_root):
        raise ConfigError("MCP settings must not live inside the coordinator root")

    repositories = parse_repositories(
        data.get("repositories", {}),
        coordination_root,
        workspace_root,
    )
    _require_unique_repository_git_identities(repositories)
    providers = parse_providers(data.get("providers", {}), coordination_root, workspace_root)
    timeout_caps = parse_timeout_caps(data.get("timeoutCaps", {}))
    benchmarks_enabled = parse_benchmarks_enabled(data.get("benchmarksEnabled", False))
    direct_execution_enabled = parse_direct_execution_enabled(
        data.get("directExecutionEnabled", False)
    )
    dashboard = parse_dashboard_settings(data.get("dashboard"))
    try:
        provider_degradation = parse_provider_degradation_settings(data.get("providerDegradation"))
    except ProviderDegradationSettingsError as error:
        raise ConfigError(str(error)) from error
    orchestration = parse_orchestration_settings(
        data.get("orchestration"),
        coordination_root=coordination_root,
        config_path=config_path,
    )
    retirement = parse_retirement_settings(data.get("retirement"))

    return McpRuntimeConfig(
        config_path=config_path,
        coordination_root=coordination_root,
        workspace_root=workspace_root,
        transcript_root=transcript_root,
        harness_skill_root=harness_skill_root,
        repositories=repositories,
        providers=providers,
        timeout_caps=timeout_caps,
        benchmarks_enabled=benchmarks_enabled,
        direct_execution_enabled=direct_execution_enabled,
        dashboard=dashboard,
        orchestration=orchestration,
        provider_degradation=provider_degradation,
        retirement=retirement,
    )


def _parse_repository_entry(
    repo_id: str,
    value: object,
    coordination_root: Path,
    workspace_root: Path,
) -> RepositoryScope:
    if not isinstance(value, dict):
        raise ConfigError(f"repository settings for {repo_id!r} must be an object")

    repo_path = workspace_root / repo_id
    memory_root = default_memory_root(repo_path, coordination_root, repo_id)
    contract_path = optional_coordination_path(
        value,
        "contractPath",
        owner=f"repository {repo_id}",
        coordination_root=coordination_root,
    )
    if contract_path is not None and not path_is_relative_to(contract_path, coordination_root):
        raise ConfigError(f"repository {repo_id} contractPath must be inside the coordinator root")
    certification_profile = _optional_repository_profile_reference(repo_id, value)
    # memorySettingsIncludes was dead plumbing (parsed, never consumed); it was
    # removed with 260703-L13. A leftover key in an existing settings file is
    # tolerated-ignored like any other unknown repository field.
    return RepositoryScope(
        repo_id=repo_id,
        path=repo_path,
        memory_root=memory_root,
        contract_path=contract_path,
        certification_profile=certification_profile,
    )


def _optional_repository_profile_reference(
    repo_id: str,
    value: dict[str, Any],
) -> Path | None:
    raw = value.get("certificationProfile")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ConfigError(
            f"repository {repo_id} certificationProfile must be one non-empty relative path"
        )
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or "\\" in raw
        or (path.parts and len(path.parts[0]) >= 2 and path.parts[0][1] == ":")
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConfigError(
            f"repository {repo_id} certificationProfile must be a canonical, "
            "traversal-free repository-relative POSIX path"
        )
    return Path(*path.parts)


def parse_repositories(
    raw: object,
    coordination_root: Path,
    workspace_root: Path,
) -> dict[str, RepositoryScope]:
    if not isinstance(raw, dict):
        raise ConfigError("repositories must be an object keyed by repo id")

    repositories: dict[str, RepositoryScope] = {}
    for repo_id, value in raw.items():
        if not isinstance(repo_id, str) or not repo_id:
            raise ConfigError("repository ids must be non-empty strings")
        repositories[repo_id] = _parse_repository_entry(
            repo_id, value, coordination_root, workspace_root
        )
    return repositories


def _require_unique_repository_git_identities(
    repositories: dict[str, RepositoryScope],
) -> None:
    """Refuse two configured repository ids that own one physical Git repository."""

    owners: dict[Path, tuple[str, str]] = {}
    for repo_id, repository in repositories.items():
        code_identity = _configured_git_identity(repository.path)
        if code_identity is not None:
            _claim_configured_git_identity(owners, code_identity, repo_id, "code")
        memory_identity = (
            _configured_git_identity(repository.memory_root)
            if repository.memory_root is not None
            else None
        )
        # An internal memory directory is intentionally part of the code repository, not
        # a second external-memory authority edge.
        if memory_identity is not None and memory_identity != code_identity:
            _claim_configured_git_identity(owners, memory_identity, repo_id, "memory")


def _configured_git_identity(path: Path) -> Path | None:
    if not path.exists():
        return None
    result = run_git(path, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def _claim_configured_git_identity(
    owners: dict[Path, tuple[str, str]],
    identity: Path,
    repo_id: str,
    side: str,
) -> None:
    previous = owners.get(identity)
    if previous is not None and previous[0] != repo_id:
        raise ConfigError(
            f"repository ids {previous[0]!r} ({previous[1]}) and {repo_id!r} ({side}) "
            f"share Git common-dir {identity.as_posix()}; one physical repository must "
            "have exactly one configured owner"
        )
    owners[identity] = (repo_id, side)


def default_memory_root(repo_path: Path, coordination_root: Path, repo_id: str) -> Path:
    internal_memory_root = repo_path / "ar-memory"
    if internal_memory_root.exists():
        return internal_memory_root.resolve()
    return coordination_root / "memory-repos" / f"ar-{repo_id}"


def infer_harness_skill_root(config_path: Path) -> Path | None:
    if config_path.parent.name != "mcp":
        return None
    return (config_path.parent.parent / "skills").resolve()


def parse_providers(
    raw: object,
    coordination_root: Path,
    workspace_root: Path,
) -> dict[str, ProviderScope]:
    if not isinstance(raw, dict):
        raise ConfigError("providers must be an object keyed by provider id")

    providers: dict[str, ProviderScope] = {}
    for provider_id, value in raw.items():
        if not isinstance(provider_id, str) or not provider_id:
            raise ConfigError("provider ids must be non-empty strings")
        if not isinstance(value, dict):
            raise ConfigError(f"provider settings for {provider_id!r} must be an object")

        provider_config = parse_provider_config(value, workspace_root)
        unsupported = sorted(set(value) - {"instanceId", "scope"})
        if unsupported:
            unsupported_text = ", ".join(unsupported)
            raise ConfigError(
                f"provider {provider_id} settings are derived by the server; "
                f"remove unsupported fields: {unsupported_text}"
            )
        provider_name = provider_runtime_name(provider_id)

        providers[provider_id] = ProviderScope(
            provider_id=provider_id,
            runtime_root=(
                coordination_root
                / "providers"
                / "runners"
                / provider_name
                / provider_config["instance_id"]
            ),
            log_root=(
                coordination_root
                / "logs"
                / "providers"
                / provider_name
                / provider_config["instance_id"]
            ),
            instance_id=provider_config["instance_id"],
            scope=provider_config["scope"],
        )
    return providers


def parse_provider_config(
    value: dict[str, Any],
    workspace_root: Path,
) -> dict[str, str]:
    scope = value.get("scope", "workspace")
    if not isinstance(scope, str) or not scope:
        raise ConfigError("provider scope must be a non-empty string")
    # scope is an open set (workspace/worktree/benchmark + custom benchmark scopes);
    # provider_instance_id slugifies it internally, so do not pre-mangle it here.
    raw_instance = value.get("instanceId")
    if raw_instance is None:
        instance_id = provider_instance_id(scope, workspace_root)
    elif not isinstance(raw_instance, str) or not raw_instance:
        raise ConfigError("provider instanceId must be a non-empty string")
    else:
        instance_id = explicit_provider_instance_id(raw_instance)
    return {"scope": scope, "instance_id": instance_id}


def provider_runtime_name(provider_id: str) -> str:
    names = {
        "codegraphcontext-code": "codegraphcontext",
        "grepai-memory": "grepai",
    }
    try:
        return names[provider_id]
    except KeyError as error:
        raise ConfigError(f"unsupported provider id: {provider_id}") from error


def parse_direct_execution_enabled(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError("directExecutionEnabled must be a boolean")
    return raw


def parse_benchmarks_enabled(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError("benchmarksEnabled must be a boolean")
    return raw


def parse_dashboard_settings(raw: object) -> DashboardSettings:
    if raw is None:
        return DashboardSettings()
    if not isinstance(raw, dict):
        raise ConfigError("dashboard settings must be an object")
    unknown = sorted(set(raw) - KNOWN_DASHBOARD_FIELDS)
    if unknown:
        allowed = ", ".join(sorted(KNOWN_DASHBOARD_FIELDS))
        unknown_text = ", ".join(unknown)
        raise ConfigError(f"unsupported dashboard setting(s): {unknown_text}; allowed: {allowed}")
    auto_start = raw.get("autoStart", False)
    if not isinstance(auto_start, bool):
        raise ConfigError("dashboard.autoStart must be a boolean")
    port = raw.get("port", DEFAULT_DASHBOARD_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError("dashboard.port must be an integer in 1..65535")
    return DashboardSettings(auto_start=auto_start, port=port)


def parse_retirement_settings(raw: object) -> RetirementSettings:
    if raw is None:
        return RetirementSettings()
    if not isinstance(raw, dict):
        raise ConfigError("retirement settings must be an object")
    unknown = sorted(set(raw) - KNOWN_RETIREMENT_FIELDS)
    if unknown:
        allowed = ", ".join(sorted(KNOWN_RETIREMENT_FIELDS))
        unknown_text = ", ".join(unknown)
        raise ConfigError(f"unsupported retirement setting(s): {unknown_text}; allowed: {allowed}")
    for key in sorted(KNOWN_RETIREMENT_FIELDS):
        if key in raw and not isinstance(raw[key], bool):
            raise ConfigError(f"retirement.{key} must be a boolean")
    auto_land_on_integration = raw.get(
        "autoLandOnIntegration", raw.get("autoRetireOnIntegration", True)
    )
    auto_land_on_finalize = raw.get("autoLandOnFinalize", raw.get("autoRetireOnFinalize", True))
    return RetirementSettings(
        auto_land_on_integration=auto_land_on_integration,
        auto_land_on_finalize=auto_land_on_finalize,
        auto_close_completed_seats=raw.get("autoCloseCompletedSeats", True),
    )


def parse_orchestration_settings(
    raw: object,
    *,
    coordination_root: Path,
    config_path: Path,
) -> OrchestrationSettings:
    """Resolve the boot-snapshot orchestration settings (260703-L13, GQ1).

    ``orchestration.gateDelegation``'s home is the GLOBAL agentic settings file
    (``<coordinationRoot>/system/settings.json``), read once at boot through the
    kernel agentic-settings loader -- a change still needs a restart (documented
    boot-snapshot semantics; the downstream ``gate_policy`` wiring is
    unchanged). An authority-file value is honored as a one-cycle legacy
    fallback WITH a boot warning naming the new home; when the global file also
    sets the key, the global value wins and the shadowed authority value warns.
    Any other ``orchestration.*`` key in the authority file fails loud -- those
    knobs never belonged here (loops) or moved home (roles/concurrency/spawn).
    """
    legacy = _parse_legacy_authority_gate_delegation(raw, coordination_root, config_path)
    try:
        agentic = load_agentic_settings(coordination_root)
    except AgenticSettingsError as error:
        raise ConfigError(str(error)) from error
    if agentic.gate_delegation_configured:
        if legacy is not None:
            _warn_legacy_gate_delegation(config_path, coordination_root, shadowed=True)
        return OrchestrationSettings(
            gate_policy=agentic.gate_policy,
            require_reviewer_verdict_at_seams=agentic.require_reviewer_verdict_at_seams,
        )
    if legacy is not None:
        _warn_legacy_gate_delegation(config_path, coordination_root, shadowed=False)
        return legacy
    return OrchestrationSettings()


def _parse_legacy_authority_gate_delegation(
    raw: object,
    coordination_root: Path,
    config_path: Path,
) -> OrchestrationSettings | None:
    """The authority file's ``orchestration`` block: gateDelegation-only, legacy."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("orchestration settings must be an object")
    unknown = sorted(set(raw) - KNOWN_AUTHORITY_ORCHESTRATION_FIELDS)
    if unknown:
        unknown_text = ", ".join(unknown)
        raise ConfigError(
            f"unsupported orchestration setting(s): {unknown_text}; the MCP "
            "authority file only reads the legacy gateDelegation fallback -- "
            "agentic orchestration settings (gateDelegation, loops, roles, "
            "rolesPerLevel, concurrency, spawn, harnesses) live in "
            f"{agentic_settings_path(coordination_root)}"
        )
    gate_raw = raw.get("gateDelegation")
    if gate_raw is None:
        return None
    try:
        gate_policy, require_verdict_at_seams = parse_gate_delegation(
            gate_raw, source=str(config_path)
        )
    except AgenticSettingsError as error:
        raise ConfigError(str(error)) from error
    return OrchestrationSettings(
        gate_policy=gate_policy,
        require_reviewer_verdict_at_seams=require_verdict_at_seams,
    )


def _warn_legacy_gate_delegation(
    config_path: Path,
    coordination_root: Path,
    *,
    shadowed: bool,
) -> None:
    """One-cycle migration warning (GQ1): the authority-file key must move home."""
    new_home = agentic_settings_path(coordination_root)
    if shadowed:
        message = (
            f"orchestration.gateDelegation in the MCP authority file ({config_path}) "
            f"is IGNORED: the global agentic settings file ({new_home}) already "
            "defines it -- remove the authority-file copy"
        )
    else:
        message = (
            "orchestration.gateDelegation moved to the global agentic settings "
            f"file ({new_home}); the MCP authority file value ({config_path}) is "
            "honored as a one-cycle legacy fallback -- move it now"
        )
    warnings.warn(message, stacklevel=2)


def parse_timeout_caps(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ConfigError("timeoutCaps must be an object")
    if "providerSeconds" in raw:
        raise ConfigError(
            "timeoutCaps.providerSeconds was renamed to providerSetupSeconds; "
            "indexing and seed are now always uncapped"
        )

    parsed: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ConfigError("timeout cap names must be non-empty strings")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"timeout cap {key!r} must be a non-negative integer")
        if key not in KNOWN_TIMEOUT_CAPS:
            allowed = ", ".join(sorted(KNOWN_TIMEOUT_CAPS))
            raise ConfigError(f"unsupported timeout cap {key!r}; allowed: {allowed}")
        parsed[key] = value
    return parsed


def required_absolute_path(data: dict[str, Any], key: str, *, owner: str = "MCP settings") -> Path:
    if key not in data:
        raise ConfigError(f"{owner} must define {key}")
    return require_absolute_json_path(data[key], f"{owner}.{key}")


def optional_absolute_path(data: dict[str, Any], key: str, *, owner: str) -> Path | None:
    if key not in data or data[key] is None:
        return None
    return require_absolute_json_path(data[key], f"{owner}.{key}")


def optional_coordination_path(
    data: dict[str, Any],
    key: str,
    *,
    owner: str,
    coordination_root: Path,
) -> Path | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{owner}.{key} must be a non-empty string")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (coordination_root / path).resolve()
    if not path_is_relative_to(resolved, coordination_root):
        raise ConfigError(f"{owner} {key} must be inside the coordinator root")
    return resolved


def require_absolute_json_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    return path.resolve()


def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _config_for_execution(config_path: str | Path) -> Path | McpRuntimeConfig:
    try:
        checkout = checkout_coordination.checkout_cli_location()
    except checkout_coordination.CheckoutCoordinationError as error:
        raise ConfigError(str(error)) from error
    if checkout is None:
        return require_config_path(config_path)
    return _checkout_runtime_config(
        checkout_root=checkout.checkout_root,
        worktree_group=checkout.worktree_group,
        coordination_root=checkout.coordination_root,
        synthetic_config_path=checkout.synthetic_config_path,
    )


def _read_config_mapping(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"cannot parse MCP settings JSON: {path}: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"MCP settings must be a JSON object: {path}")
    return data


def _checkout_runtime_config(
    *,
    checkout_root: Path,
    worktree_group: Path,
    coordination_root: Path,
    synthetic_config_path: Path,
) -> McpRuntimeConfig:
    """Build the minimal non-authority config for an unpublished linked checkout."""
    repository = RepositoryScope(
        repo_id="agents-remember",
        path=checkout_root,
        memory_root=coordination_root / "memory-repos" / "ar-agents-remember",
    )
    return McpRuntimeConfig(
        config_path=synthetic_config_path,
        coordination_root=coordination_root,
        workspace_root=worktree_group,
        transcript_root=coordination_root / "logs" / "mcp",
        repositories={"agents-remember": repository},
        providers={},
        benchmarks_enabled=False,
        direct_execution_enabled=False,
        dashboard=DashboardSettings(auto_start=False),
        orchestration=OrchestrationSettings(),
        retirement=RetirementSettings(
            auto_land_on_integration=False,
            auto_land_on_finalize=False,
            auto_close_completed_seats=False,
        ),
    )
