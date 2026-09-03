"""The dashboard FastAPI app: one-shot state, the multiplexed SSE stream, raw events, actions.

Endpoints:

* ``GET  /api/state``           -- the current projection once (curl-friendly, no streaming).
* ``GET  /api/stream``          -- one EventSource: an ``event: snapshot`` with the full
  projection on connect, then per-entity ``state`` deltas (``lifecycle`` / ``enclosure`` /
  ``provider`` / ``metrics`` / ``analytics`` and their ``*.removed`` markers) fanned out from
  the shared :class:`Projector`.
* ``GET  /api/events``          -- the raw ``ar-observer-event/v1`` log, tailed with exact
  byte-offset ``Last-Event-ID`` resume. A *separate* stream from ``/api/stream``:
  it resumes by byte offset, the state channel re-snapshots, so mixing them on one stream
  would be incoherent. The cockpit opens both (still well under the ~6 connections/origin cap).
* ``POST /api/actions/{action}``-- the gate return-channel. Lifecycle transitions
  (``resume`` / ``integrate`` / ``cleanup``) are validated against the reducer's
  ``ActionAvailability`` and acknowledged without mutation; gate-decision verbs
  (``approve`` / ``reject`` / ``request-revision`` / ``cancel``) are recorded as
  developer-attributed gate decisions, which server-side closeout enforcement
  makes binding. Routing maps :class:`ActionOutcome` onto the response; see ``serving/actions.py``.
* ``POST /api/operator-inbox``  -- the external-chat gate response return channel. The
  dashboard writes a developer response to the append-only operator inbox when no hosted chat
  session can be injected into; external agents poll/consume through the MCP ``operator_inbox_*``
  tools.

Local-first posture: bind ``127.0.0.1`` only (the CLI default) with no auth in v1. This is a
cockpit for the developer's own machine; exposing it (an SSH tunnel, a reverse proxy) hands an
unauthenticated reader the whole projection -- and the POST action surface -- so any multi-user
or remote story is a deliberate later design with its own auth gate.

The ``now`` / ``before_tick`` parameters are the sim seams: live serving leaves them
at their defaults; ``cli.dashboard`` passes a replay clock + fixture feeder under ``--sim``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from agents_remember.kernel.agentic_settings import load_agentic_settings
from agents_remember.observer import observer_root
from agents_remember.providers.metrics import ProviderMetricsStore
from agents_remember.serving._app_common import (
    _IMAGE_EXTS,
    _MAX_IMAGE_BYTES,
    _TERMINAL_EXIT_FRAME,
    DEFAULT_SHELL,
    INFERRED_LIVE_INPUTS,
    LiveProjectionInputs,
    OperatorInboxPostRequest,
    ServingCollaborators,
    TerminalAttachTaskRequest,
    TerminalLandedCleanupRequest,
    TerminalOpenRequest,
    TerminalPasteRequest,
    TerminalRenameRequest,
    TerminalRetireRequest,
    _apply_terminal_input,
    _attach_terminal_session,
    _bridge_terminal,
    _catalog_payload,
    _encode,
    _if_none_match_matches,
    _looks_like_image,
    _projection_body_cache,
    _ProjectionBodyCache,
    _ResolvedLiveInputs,
    _ServingRuntime,
    _socket_to_terminal,
    _terminal_to_socket,
    logger,
    stream_events,
)
from agents_remember.serving._app_lifespan import (
    _agent_notifier_context,
    _agent_notifier_heartbeat_payload,
    _agent_notifier_loop,
    _malloc_trim_loop,
    _metrics_loop,
    _serving_lifespan,
    _workspace_river_compaction_loop,
)
from agents_remember.serving._app_routes import (
    _action_response,
    _dismissal_response,
    _gate_decision_response,
    _inbox_dismiss_response,
    _operator_inbox_response,
    _recorded_gate_decision,
    _register_action_routes,
    _register_projection_routes,
    _state_response,
    _task_document_response,
)
from agents_remember.serving._app_terminal_routes import (
    _attach_task_response,
    _detected_harnesses_payload,
    _harness_submit_response,
    _landed_cleanup_response,
    _live_paste_target,
    _open_terminal_response,
    _pane_paste_response,
    _paste_response,
    _register_terminal_control_routes,
    _register_terminal_session_routes,
    _rename_response,
    _retire_response,
    _seat_ref,
    _serve_terminal_websocket,
    _terminal_entry_payload,
    _terminal_image_response,
    _terminate_response,
    _write_paste_image,
)
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.build_info import process_serving_build
from agents_remember.serving.change_watcher import ProjectionInputWatcher
from agents_remember.serving.changeset import register_changeset_routes
from agents_remember.serving.conversation.authorization import LocalOperatorAuthorizationResolver
from agents_remember.serving.conversation.runtime import ConversationRuntime, ConversationScope
from agents_remember.serving.files import register_files_routes
from agents_remember.serving.harness_capability_catalog import HarnessCapabilityCatalog
from agents_remember.serving.harness_control_api import register_harness_control_routes
from agents_remember.serving.harness_control_client import ControlPlaneClient
from agents_remember.serving.hosted_interactions import HostedInteractionSynchronizer
from agents_remember.serving.notes import register_notes_routes
from agents_remember.serving.projections.landing_state import LandingStateRefresher
from agents_remember.serving.projections.projection_store import ProviderStateRefresher
from agents_remember.serving.projector import (
    DEFAULT_PROJECTION_CADENCE,
    LIVE_PROJECTION_CLOCK,
    ProjectionCadence,
    ProjectionRefreshers,
    ProjectionReplay,
    Projector,
)
from agents_remember.serving.requirements import register_requirements_routes
from agents_remember.serving.seat_events import log_turn_state_change_event
from agents_remember.serving.static import mount_static
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
    terminal_catalog_path,
)
from agents_remember.serving.terminal_liveness import (
    LivenessProbe,
    TerminalCatalogLivenessConfig,
    TerminalCatalogLivenessSweeper,
    TerminalLivenessActions,
    utc_now,
)
from agents_remember.serving.terminal_paste import TerminalPaster

if TYPE_CHECKING:
    from agents_remember.kernel.primitives.runtime_config import (
        McpRuntimeConfig,
    )

OWNED_SERVING_COLLABORATORS = ServingCollaborators()
"""No injected collaborators: the app constructs and owns every one of them."""


def _build_serving_runtime(
    config: McpRuntimeConfig,
    cadence: ProjectionCadence,
    replay: ProjectionReplay,
    live_inputs: LiveProjectionInputs,
    collaborators: ServingCollaborators,
) -> tuple[_ServingRuntime, ProviderMetricsStore]:
    """Construct the long-lived collaborators one serving app owns, before any route exists.

    Separated from ``create_app`` because the two halves answer different questions: this is
    what the process will be running, and ``create_app`` is what it will serve. The metrics
    store rides back beside the runtime rather than on it -- the lifespan owns its sampling
    loop, and nothing that handles a request reads it.
    """
    enabled = live_inputs.resolved(replay)
    projector = Projector(
        config,
        cadence=cadence,
        replay=replay,
        refreshers=ProjectionRefreshers(
            provider=ProviderStateRefresher() if enabled.provider_state else None,
            landing=LandingStateRefresher(config) if enabled.landing_state else None,
            change_watcher=ProjectionInputWatcher(config) if enabled.change_watch else None,
        ),
    )
    host = collaborators.terminal_host or TerminalHost()
    catalog = collaborators.terminal_catalog or TerminalCatalog(
        terminal_catalog_path(config.coordination_root)
    )
    liveness_config = TerminalCatalogLivenessConfig()
    interaction_synchronizer = HostedInteractionSynchronizer(observer_root(config))
    terminal_execution_registrar = collaborators.register_terminal_execution_evidence
    liveness_sweeper = TerminalCatalogLivenessSweeper(
        catalog,
        host,
        now=replay.now,
        probe=LivenessProbe(
            hysteresis=liveness_config,
            on_control_snapshot=interaction_synchronizer.observe,
        ),
        actions=TerminalLivenessActions(
            on_turn_state_change=lambda observation: log_turn_state_change_event(
                config, observation.entry
            ),
            register_execution_evidence=(
                None
                if terminal_execution_registrar is None
                else lambda entries: terminal_execution_registrar(
                    config.coordination_root,
                    entries,
                )
            ),
        ),
    )
    runtime = _ServingRuntime(
        config=config,
        projector=projector,
        host=host,
        catalog=catalog,
        paster=collaborators.terminal_paster or TerminalPaster(),
        liveness_clock=replay.now or utc_now,
        liveness_config=liveness_config,
        liveness_sweeper=liveness_sweeper,
        # Resolved ONCE at boot: the stamp that makes a stale serving process visible.
        build=process_serving_build(),
        # The deterministic agent-notifier sweep runs on its own decoupled cadence
        # (default ~10s, settings-controlled), zero tokens, pure code. "The model is never the
        # polling layer": every predicate reads TerminalCatalog/OperatorInboxStore/
        # ExpectationRowStore DIRECTLY, never the projection.
        heartbeat_store=AgentNotifierHeartbeatStore(observer_root(config)),
        register_inbox_execution_evidence=collaborators.register_inbox_execution_evidence,
        interval=cadence.interval,
    )
    # The serving daemon samples labeled provider
    # containers on its own cadence (decoupled from the 1s projection tick) into
    # the central metrics store — the feed for provider_status, the statistics
    # board, and the degradation protocol. Read-only + dockerless-safe.
    return runtime, ProviderMetricsStore(config.coordination_root)


def create_app(
    config: McpRuntimeConfig,
    *,
    cadence: ProjectionCadence = DEFAULT_PROJECTION_CADENCE,
    replay: ProjectionReplay = LIVE_PROJECTION_CLOCK,
    live_inputs: LiveProjectionInputs = INFERRED_LIVE_INPUTS,
    collaborators: ServingCollaborators = OWNED_SERVING_COLLABORATORS,
) -> FastAPI:
    """Build the dashboard app bound to one shared projector for ``config``.

    ``replay`` defaults to live behaviour; sim wires a replay clock + feeder. Live serving enables
    the landing-state refresher and the change-driven projection watcher by default; sim disables
    both unless ``live_inputs`` says otherwise (replay must stay time-driven -- the sim feeder only
    writes *inside* a tick, so a change-gated loop would never wake). ``cadence.interval`` is the
    fast-path projection cadence floor; ``cadence.heartbeat`` bounds quiet-world ``/api/state``
    staleness (default ``DEFAULT_HEARTBEAT_SECONDS``). ``collaborators`` default to freshly
    constructed ones (the Mode B2 terminal backend and friends); tests inject fakes to drive the
    WebSocket bridge without a real PTY.
    """
    runtime, metrics_store = _build_serving_runtime(
        config, cadence, replay, live_inputs, collaborators
    )
    app = FastAPI(
        title="Agents Remember dashboard",
        lifespan=_serving_lifespan(runtime, metrics_store),
    )
    # Gzip the multi-hundred-KB JSON bodies (/api/state ~1.3 MB, the files
    # catalog) for the clients that negotiate it. compresslevel=6: on a ~1.3 MB JSON body it
    # matches level 9's ratio for ~16% less CPU per serve. Starlette's responder excludes
    # text/event-stream by content type, so the SSE channels (/api/stream, /api/events, the
    # conversation event streams) keep streaming uncompressed and unbuffered (covered by test).
    app.add_middleware(GZipMiddleware, compresslevel=6)
    _register_projection_routes(app, runtime)
    _register_action_routes(app, runtime)
    _register_terminal_session_routes(app, runtime)
    _register_terminal_control_routes(app, runtime)
    register_files_routes(app, config)
    register_changeset_routes(app, config)
    register_notes_routes(app, config)
    register_requirements_routes(app, config)
    register_harness_control_routes(
        app,
        ConversationRuntime(
            scope=ConversationScope(
                workspace_root=config.workspace_root,
                coordination_root=config.coordination_root,
            ),
            catalog=runtime.catalog,
            control_plane=ControlPlaneClient(),
            host=runtime.host,
            harness_registry=lambda: load_agentic_settings(config.coordination_root).harnesses,
            liveness_clock=runtime.liveness_clock,
            liveness_config=runtime.liveness_config,
            capability_catalog=(
                collaborators.harness_capability_catalog
                or HarnessCapabilityCatalog(config.workspace_root)
            ),
            authorization=LocalOperatorAuthorizationResolver.for_workspace(config.workspace_root),
        ),
    )
    mount_static(app)
    return app


# --- background loops ---------------------------------------------------------------------------


# --- terminal session routes --------------------------------------------------------------------

__all__ = [
    "DEFAULT_SHELL",
    "INFERRED_LIVE_INPUTS",
    "OWNED_SERVING_COLLABORATORS",
    "_IMAGE_EXTS",
    "_MAX_IMAGE_BYTES",
    "_TERMINAL_EXIT_FRAME",
    "LiveProjectionInputs",
    "OperatorInboxPostRequest",
    "ServingCollaborators",
    "TerminalAttachTaskRequest",
    "TerminalLandedCleanupRequest",
    "TerminalOpenRequest",
    "TerminalPasteRequest",
    "TerminalRenameRequest",
    "TerminalRetireRequest",
    "_ProjectionBodyCache",
    "_ResolvedLiveInputs",
    "_ServingRuntime",
    "_action_response",
    "_agent_notifier_context",
    "_agent_notifier_heartbeat_payload",
    "_agent_notifier_loop",
    "_apply_terminal_input",
    "_attach_task_response",
    "_attach_terminal_session",
    "_bridge_terminal",
    "_build_serving_runtime",
    "_catalog_payload",
    "_detected_harnesses_payload",
    "_dismissal_response",
    "_encode",
    "_gate_decision_response",
    "_harness_submit_response",
    "_if_none_match_matches",
    "_inbox_dismiss_response",
    "_landed_cleanup_response",
    "_live_paste_target",
    "_looks_like_image",
    "_malloc_trim_loop",
    "_metrics_loop",
    "_open_terminal_response",
    "_operator_inbox_response",
    "_pane_paste_response",
    "_paste_response",
    "_projection_body_cache",
    "_recorded_gate_decision",
    "_register_action_routes",
    "_register_projection_routes",
    "_register_terminal_control_routes",
    "_register_terminal_session_routes",
    "_rename_response",
    "_retire_response",
    "_seat_ref",
    "_serve_terminal_websocket",
    "_serving_lifespan",
    "_socket_to_terminal",
    "_state_response",
    "_task_document_response",
    "_terminal_entry_payload",
    "_terminal_image_response",
    "_terminal_to_socket",
    "_terminate_response",
    "_workspace_river_compaction_loop",
    "_write_paste_image",
    "create_app",
    "logger",
    "stream_events",
]
