"""The declared response contract for every HTTP route the serving app registers.

**Why this module exists at all.** Not one of the original 61 HTTP routes declared a
``response_model``. Twenty-five of them (the conversation surface) already *dumped* a strict
:class:`~agents_remember.models.conversations.primitives.WireModel`, which proves a model existed --
not that the route declared its contract. Nothing anywhere said what
``GET /api/files/read`` answers with, and nothing could fail when that answer drifted.

**Why the declaration alone is not the gate.** FastAPI applies ``response_model`` only to
values it serializes itself: ``fastapi.routing.get_request_handler`` returns a ``Response``
instance untouched and never reaches ``serialize_response``. Of the 63 handlers, **59** return
a ``Response`` subclass (``JSONResponse``/``Response``/``StreamingResponse``) directly and
**two** -- ``GET /api/stream`` and ``GET /api/events`` -- are async generators feeding an
``EventSourceResponse``; on all 59 the decorator contributes an OpenAPI schema and **validates
nothing**. Only ``GET /api/terminal/sessions`` and ``GET /api/harnesses`` return a bare
``dict`` and are therefore validated by FastAPI itself. "Declare a model everywhere plus a
test that no declaration is missing" would have been green on day one and would have enforced
nothing on 59 routes.

So the declarations here are the *contract*, and the enforcement is
``mcp/tests/test_serving_response_conformance.py``: it drives every route through the real
app and validates the body that actually came back against the model the route declares.

**What the decorator DOES do on those two routes, which is a behaviour change.** Before they
declared a model, ``GET /api/terminal/sessions`` and ``GET /api/harnesses`` were
forward-compatible pass-through: the dict the handler built was serialized as-is, and a key
the model did not know about simply reached the cockpit. With ``response_model`` plus
``extra="forbid"`` they now answer **HTTP 500** (``ResponseValidationError``) if the payload
gains a key, loses a required one, or changes a type. That is a deliberate fail-loud trade and
not a silent one -- but it is a real hazard on ``/api/terminal/sessions``, because
``TerminalCatalogEntry`` carries 56 optional fields and is actively grown: a future leaf adding
one to ``to_json`` and forgetting ``TerminalCatalogEntryWire`` would take down the cockpit's
session list, not degrade it.

The mitigation is that the failure is moved off the wire and into CI, where the rest of this
contract already lives: ``test_the_catalog_wire_model_covers_every_key_to_json_emits`` scans
``TerminalCatalogEntry.to_json``'s emitted key set and asserts set EQUALITY against this
model's aliases. That fires the moment the field is added, before any payload carries it --
which is strictly earlier than either the runtime 500 or a conformance run whose fixture
happens to populate the new field. The runtime check is kept on top of it because it is the
one place on this whole surface where a body is validated for real; what it must not be is the
*first* thing that notices, and it no longer is.
The conversation surface's own additions live in
``serving/conversation/response_contract.py`` -- they need ``conversation/models.py``, and
this module must stay importable *before* that package exists, because ``serving/app.py``
imports the file/change-set/notes routes first.
Rewriting the 59 handlers to return models instead of ``Response`` objects would hand the
enforcement to FastAPI, but it cannot be done under R11 -- the bare ``304`` branch on
``/api/state``, the per-status typed refusals, and the 200/202/422 fan-out of
``/conversation/submit`` all choose a status *and* a body shape that a single return type
cannot express.

**Multi-shape routes.** A route that answers with more than one shape declares the success
shape as ``response_model`` and every refusal shape under ``responses={...}``; the union
members are the exact bodies the handler can emit, not a generic error envelope. The route
tables below (``*_RESPONSES``) are shared because the refusal idiom is shared: the files /
notes / change-set family maps its domain errors through one status vocabulary, and every
conversation route routes its typed failures through ``_map_typed_error``.

**Exemptions, by structure and not by name.** ``WS /api/terminal/{session}`` is registered as
a ``fastapi.routing.APIWebSocketRoute``, which has no ``response_model`` parameter at all --
a websocket has no response body to model. That is the only route without a declaration, and
the exhaustiveness test finds it by its route *class*, never by a path skip-list (a skip-list
would silently absorb the next undeclared HTTP route).

Every model here is strict (``extra="forbid"``), immutable, and camel-aliased, mirroring
``serving/conversation/models.WireModel`` -- an undeclared key is a failure, which is the
entire point.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agents_remember.models.structural.gates import GateDecideResponse
from agents_remember.models.task_document_ref import TaskDocumentRef

__all__ = [
    "ACTION_RESPONSES",
    "SCOPED_READ_RESPONSES",
    "SESSION_CONTROL_RESPONSES",
    "WireResponse",
]


class WireResponse(BaseModel):
    """Strict immutable camel-case response model, mirroring the conversation ``WireModel``.

    ``extra="forbid"`` is what makes a conformance check able to fail: a key the handler
    started emitting and nobody declared is a validation error, not a silent addition.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


# --- refusal shapes ------------------------------------------------------------------------
#
# The serving status idiom: every refusal names itself in ``status`` and explains itself in
# ``detail``. The variants below differ only in which identifier the refusal echoes back, and
# each is a separate model because ``extra="forbid"`` means a shared "status + anything" model
# would accept every shape and pin none of them.


class StatusRefusal(WireResponse):
    """``{status, detail}`` -- the shared refusal body of every serving surface."""

    status: str
    detail: str


class UnknownRepoRefusal(WireResponse):
    """404 from the repo allow-list: the boundary echoes the repo it refused."""

    status: Literal["unknown-repo"]
    repo: str


class UnknownScopeRefusal(WireResponse):
    """404 from scope resolution: the enclosure/mainline scope that resolved to nothing."""

    status: Literal["unknown-scope"]
    scope: str


class MissingPathRefusal(WireResponse):
    """404 from a confined read: the path (or the missing artifact) that was not there."""

    status: Literal["not-found"]
    path: str


class UnknownSessionRefusal(WireResponse):
    """404 from a seat lookup -- a bare status, because the session id is already in the URL."""

    status: Literal["unknown-session"]


class UnsupportedSeatRefusal(WireResponse):
    """409 from a seat with no native protocol control endpoint."""

    status: Literal["unsupported"]
    detail: str


class BridgeEpochMismatchRefusal(WireResponse):
    """409 from the epoch pre-check: both epochs cross so the caller can resynchronize."""

    status: Literal["bridge-epoch-mismatch"]
    detail: str
    expected_bridge_epoch: str
    actual_bridge_epoch: str


class PreDispatchFailureRefusal(WireResponse):
    """503 from the one control client that certified zero socket bytes: safe to retry."""

    status: Literal["pre-dispatch-failed"]
    detail: str
    retry_safe: bool
    stage: str


class CapabilityUnavailableRefusal(WireResponse):
    """422 from the library capability gate: which capability state refused the call."""

    status: Literal["capability-unavailable"]
    capability_state: str
    detail: str


class CursorRefusal(WireResponse):
    """The active-stream cursor refusals, which alone may carry a machine-readable reason."""

    status: str
    detail: str
    reason: str | None = None


class HttpDetailRefusal(BaseModel):
    """FastAPI's own ``HTTPException`` body -- not ours, but it is what the wire carries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: str


# --- /api/state, /api/task-document, /api/stream, /api/events ------------------------------


class StreamReadyMarker(WireResponse):
    """The one frame ``/api/events`` mints itself: the backlog has drained.

    Every other frame on that stream is a verbatim observer JSONL record replayed from disk
    (``events.read_new_events`` parses it and re-emits the parsed object unchanged), so the
    river's schema belongs to the observer, not to this route. What the route itself promises
    is this marker, and that its own frames never invent a shape.
    """

    ready: Literal[True]


# --- /api/actions/{action} ------------------------------------------------------------------


class ActionIntent(WireResponse):
    """The attributed intent echoed back by every accepted action.

    ``actor``/``source``/``ts``/``action`` are always written; the rest are added only when
    the verb has them, which is why they are optional here rather than nullable.
    """

    actor: str
    source: Literal["dashboard"]
    ts: str
    action: str
    target: str | None = None
    item_id: str | None = None
    kind: str | None = None
    gate_id: str | None = None
    note: str | None = None


class ActionAccepted(WireResponse):
    """202: the intent was recorded. ``gate`` rides along when the verb decided a gate."""

    status: Literal["received"]
    detail: str
    intent: ActionIntent
    gate: GateDecideResponse | None = None


class ActionTargetRefusal(WireResponse):
    """400/409 refusals that name the action they refused."""

    status: str
    detail: str
    action: str
    target: str | None = None
    item_id: str | None = None
    next_safe_action: str | None = None


class UnknownTargetRefusal(WireResponse):
    """404: the projection holds no such lifecycle/enclosure target."""

    status: Literal["unknown-target"]
    target: str


class ActionDetailRefusal(WireResponse):
    """400/409 refusals that name the target rather than the action."""

    status: str
    detail: str
    target: str | None = None


# --- /api/operator-inbox --------------------------------------------------------------------


class OperatorInboxDismissed(WireResponse):
    """200/404 from the dismiss route: the same two keys either way."""

    status: Literal["dismissed", "not-found"]
    entry_id: str


# --- /api/terminal/sessions + /api/harnesses ------------------------------------------------
#
# The only two routes FastAPI validates for us, because they alone return a bare ``dict``.


class TerminalCatalogEntryWire(WireResponse):
    """One durable catalog row as ``TerminalCatalogEntry.to_json`` emits it.

    ``to_json`` is hand-rolled and *conditional*: the nine head keys and ``seatRole`` are
    always written, and everything else goes through ``_present_fields`` -- present only when
    non-``None``. The field order below is that emission order exactly, and the route declares
    ``response_model_exclude_unset=True``, so re-serializing through this model reproduces the
    hand-rolled body byte for byte instead of back-filling nulls the dashboard never saw.

    This is the one model on the surface whose drift is a live 500 rather than a red test (see
    the module docstring), so it is also the one with a key-set equality test against its
    producer: a field added to ``to_json`` and not to this class fails in CI at the moment it
    is added, without needing a payload that carries it.
    """

    id: str
    label: str
    kind: Literal["terminal", "harness"]
    cwd: str
    tmux_name: str
    command: list[str]
    created_at: str
    last_attached_at: str
    status: str
    harness: str | None = None
    lifecycle_id: str | None = None
    terminated_at: str | None = None
    task_document_ref: TaskDocumentRef | None = None
    seat_role: str
    replacement_for_task_document_ref: TaskDocumentRef | None = None
    spawned_by_session: str | None = None
    spawned_by_lifecycle: str | None = None
    spawned_by_kind: str | None = None
    structural_parent_task_document_ref: TaskDocumentRef | None = None
    structural_parent_role: str | None = None
    spawn_role: str | None = None
    launch_args: list[str] | None = None
    prompt_keywords: list[str] | None = None
    session_commands: list[str] | None = None
    spawn_level: str | None = None
    spawn_level_source: str | None = None
    resolved_model: str | None = None
    resolved_effort: str | None = None
    session_log_entry_id: str | None = None
    session_log_path: str | None = None
    dispatch_brief_entry_id: str | None = None
    control_state: str | None = None
    control_endpoint: str | None = None
    control_protocol: str | None = None
    control_activity: str | None = None
    control_acceptance: str | None = None
    control_vendor_session_id: str | None = None
    control_pending_interaction: dict[str, Any] | None = None
    control_pending_interactions: list[dict[str, Any]] | None = None
    control_last_event_sequence: int | None = None
    control_raw: dict[str, Any] | None = None
    liveness_first_failed_at: str | None = None
    liveness_last_failed_at: str | None = None
    liveness_evidence: Literal["tmux-command-failed", "pane-gone"] | None = None
    retired_at: str | None = None
    retired_by_session: str | None = None
    retired_reason: str | None = None
    retired_edge: str | None = None
    landed_at: str | None = None
    landed_reason: str | None = None
    landed_edge: str | None = None
    spawned_label: str | None = None
    turn_state: str | None = None
    turn_state_changed_at: str | None = None
    terminal_outcome: Literal["completed", "interrupted", "failed", "unknown"] | None = None
    terminal_outcome_at: str | None = None
    terminal_evidence_id: str | None = None
    interrupted_by: Literal["developer", "unknown"] | None = None
    terminal_evidence_sequence: int | None = None
    terminal_native_cursor: str | None = None
    interrupt_requested_by: Literal["developer"] | None = None
    interrupt_requested_at: str | None = None
    interrupt_requested_turn_id: str | None = None
    state_signal_emitted_for: str | None = None
    non_reaction_emitted_for: str | None = None
    compound_idle_emitted_for: str | None = None
    liveness_failures: int | None = None
    exit_evidence: Literal["tmux-command-failed", "pane-gone"] | None = None


class TerminalSessionsResponse(WireResponse):
    """``GET /api/terminal/sessions``: the swept catalog."""

    sessions: list[TerminalCatalogEntryWire]


class DetectedHarness(WireResponse):
    """One supported TUI harness and whether its executable is installed here."""

    id: str
    name: str
    detected: bool


class DetectedHarnessesResponse(WireResponse):
    """``GET /api/harnesses``: the launch-button registry."""

    harnesses: list[DetectedHarness]


# --- terminal control -----------------------------------------------------------------------


class TerminalEntryFacts(WireResponse):
    """The catalog facts every open/conflict answer repeats about one durable row."""

    session: str
    kind: str
    harness: str | None = None
    tmux_name: str
    control_state: str | None = None
    control_endpoint: str | None = None
    control_protocol: str | None = None
    resolved_model: str | None = None
    resolved_effort: str | None = None


class TerminalOpened(TerminalEntryFacts):
    """200 from ``POST /api/terminal/{session}``: the row that is now running."""

    label: str
    lifecycle_id: str | None = None
    task_document_ref: TaskDocumentRef | None = None
    seat_role: str
    cwd: str
    status: Literal["running"]


class TerminalLaunchConflict(TerminalEntryFacts):
    """409: the seat exists but was launched under a different model/effort selection."""

    status: Literal["launch-selection-conflict"]
    detail: str | None = None


class SeatTakenConflict(WireResponse):
    """409 from open/attach: a document-owned role seat already has an occupant."""

    status: Literal["seat-taken"]
    task_document_ref: TaskDocumentRef
    session: str | None = None


class TerminalCleanupResult(WireResponse):
    """200 from ``POST /api/terminal/landed-cleanup``: counts plus both id lists."""

    status: Literal["cleaned"]
    closed: int
    skipped: int
    closed_sessions: list[str]
    skipped_sessions: list[TerminalCleanupSkip]


class TerminalCleanupSkip(WireResponse):
    """One seat the cleanup left alone, and why."""

    session: str
    reason: str


class TerminalTaskAttached(WireResponse):
    """200 from ``/attach-task``: the binding as it now stands."""

    session: str
    status: Literal["attached"]
    task_document_ref: TaskDocumentRef
    role: str | None = None
    seat_role: str | None = None
    previous_seat_role: str | None = None


class TerminalTaskRefused(WireResponse):
    """409/400 from ``/attach-task``: taken, roleless, or wrong-altitude binding."""

    session: str | None = None
    status: Literal["seat-taken", "role-required", "task-binding-invalid"]
    task_document_ref: TaskDocumentRef
    detail: str | None = None


class TerminalPaneDelivery(WireResponse):
    """200 from ``/paste`` on a plain terminal: transport is all this path can prove."""

    session: str
    entry_id: str
    status: Literal["delivered", "unconfirmed"]
    delivered: bool
    submitted: bool
    capture: str | None = None


class TerminalHarnessDelivery(WireResponse):
    """200 from ``/paste`` on a protocol harness: one correlated whole message's receipt."""

    session: str
    entry_id: str
    status: Literal["delivered", "unconfirmed"]
    delivered: bool
    submitted: bool
    acceptance: str | None = None
    vendor_correlation_id: str | None = None
    accepted_at: str | None = None
    detail: str | None = None


class TerminalHarnessRefusal(WireResponse):
    """409 from ``/paste``: a legacy harness seat, or a draft that was never submitted."""

    session: str
    status: Literal["unsupported", "draft-not-submitted"]
    detail: str


class TerminalTerminated(WireResponse):
    """200 from ``/terminate``: the terminal mark, plus what the stop attempt learned."""

    session: str
    status: Literal["terminated"]
    terminated_at: str
    tmux_name: str | None = None
    control_stop_detail: str | None = None


class TerminalRetired(WireResponse):
    """200 from ``/retire``: the retirement provenance the catalog now holds."""

    session: str
    status: Literal["retired"]
    retired_at: str | None = None
    retired_by_session: str | None = None
    retired_reason: str | None = None
    retired_edge: str | None = None


class TerminalAlreadyRetired(WireResponse):
    """200 from ``/retire`` on a seat that was already terminal: idempotent, never an error."""

    session: str
    status: Literal["already-retired"]
    retired_at: str | None = None


class TerminalRetireRefused(WireResponse):
    """403: retire authority refused this actor for this target."""

    session: str
    status: Literal["retire-refused"]
    detail: str


class UnknownActorRefusal(WireResponse):
    """404: the actor session named by a retire request is not in the catalog."""

    status: Literal["unknown-actor"]
    actor_session: str


class TerminalRenamed(WireResponse):
    """200 from ``/rename``: identity text only; the seat role is never touched."""

    session: str
    status: Literal["renamed"]
    label: str
    spawned_label: str | None = None


class TerminalImageStored(WireResponse):
    """200 from ``/image``: the absolute on-disk path the composer injects."""

    path: str


class TerminalImageRefusal(WireResponse):
    """400/413 from ``/image``: a bare status, because the reason is the status."""

    status: Literal["bad-type", "too-large"]


# --- files ----------------------------------------------------------------------------------


class MainlineScope(WireResponse):
    """A repository's own checkout as a browsable scope."""

    scope: Literal["mainline"]
    label: str
    exists: bool


class WorktreeScope(WireResponse):
    """One active leaf enclosure as a browsable scope."""

    scope: str
    label: str
    branch: str | None = None
    leaf_id: str | None = None
    task_name: str | None = None


class RepoCatalogRow(WireResponse):
    """One allow-listed repository with everything that can be browsed inside it."""

    repo: str
    mainline: MainlineScope
    worktrees: list[WorktreeScope]


class RepoCatalog(WireResponse):
    """``GET /api/files/repos``."""

    repos: list[RepoCatalogRow]


class PathNode(WireResponse):
    """One onboarding-tree child: name, tree-relative path, and which of the two it is."""

    name: str
    path: str
    kind: Literal["file", "dir"]


class CodeDirNode(WireResponse):
    """A code directory child -- no language and no sidecar pairing to report."""

    name: str
    path: str
    kind: Literal["dir"]


class CodeFileNode(WireResponse):
    """A code file child, annotated with its language and whether a sidecar pairs it."""

    name: str
    path: str
    kind: Literal["file"]
    language: str
    has_sidecar: bool


CodeNode = Annotated[CodeFileNode | CodeDirNode, Field(discriminator="kind")]
"""A code listing entry. The discriminator is what makes the annotation load-bearing: only a
``kind: "file"`` row is allowed to carry ``language``/``hasSidecar``, and only it must."""


class DirectoryListing(WireResponse):
    """``GET /api/files/list``: one lazily-expanded directory level, both trees."""

    scope: str
    dir: str
    code: list[CodeNode]
    onboarding: list[PathNode]


class OnboardingMissing(WireResponse):
    """No sidecar pairs this path. Normal -- the File Viewer renders a placeholder."""

    status: Literal["missing"]


class OnboardingFound(WireResponse):
    """The paired sidecar, with the drift-bearing verification stamps it carries."""

    status: Literal["found"]
    path: str
    last_verified_commit_hash: str | None = None
    last_verified_commit_date: str | None = None


OnboardingMeta = Annotated[OnboardingFound | OnboardingMissing, Field(discriminator="status")]


class FileContents(WireResponse):
    """``GET /api/files/read``: one size-capped, binary-tolerant file plus its pairing."""

    scope: str
    path: str
    language: str
    size: int
    truncated: bool
    content: str
    onboarding: OnboardingMeta


class OnboardingForwardMissing(WireResponse):
    """``direction=forward`` for a code path with no sidecar."""

    scope: str
    code_path: str
    body: str | None = None
    status: Literal["missing"]


class OnboardingForwardFound(WireResponse):
    """``direction=forward``: the sidecar's stamps and, when present, its rendered body."""

    scope: str
    code_path: str
    body: str | None = None
    status: Literal["found"]
    path: str
    last_verified_commit_hash: str | None = None
    last_verified_commit_date: str | None = None


class OnboardingPartnerNone(WireResponse):
    """``direction=reverse`` in a scope with no onboarding root at all."""

    scope: str
    onboarding_path: str
    kind: Literal["none"]


class OnboardingPartnerOverview(WireResponse):
    """``direction=reverse`` for a partnerless doc: it carries its own markdown body."""

    scope: str
    onboarding_path: str
    kind: Literal["overview"]
    route: str
    body: str | None = None


class OnboardingPartnerSidecar(WireResponse):
    """``direction=reverse`` for a file sidecar: the code path it pairs, and whether it is there."""

    scope: str
    onboarding_path: str
    kind: Literal["sidecar"]
    code_path: str
    exists: bool


OnboardingResolution = (
    OnboardingForwardFound
    | OnboardingForwardMissing
    | OnboardingPartnerSidecar
    | OnboardingPartnerOverview
    | OnboardingPartnerNone
)
"""``GET /api/files/onboarding`` answers in one of five shapes -- the ``direction`` query
picks the pair, and within each pair the ``status``/``kind`` discriminates. Declaring the
union is the honest contract; declaring only the forward shape would have been a lie about
half the route's traffic."""


# --- notes ----------------------------------------------------------------------------------


class NoteRow(WireResponse):
    """One note file under ``tasks/<repo>/<master>/notes/``."""

    name: str
    path: str
    size: int
    language: str


class NotesListing(WireResponse):
    """``GET /api/notes/list``: a series' notes; ``truncated`` when the depth cap pruned."""

    repo: str
    master: str
    notes: list[NoteRow]
    truncated: bool


class NoteContents(WireResponse):
    """``GET /api/notes/read``: one note, size-capped and binary-tolerant."""

    repo: str
    master: str
    path: str
    language: str
    size: int
    truncated: bool
    content: str


# --- task-local requirements ----------------------------------------------------------------


class RequirementRow(WireResponse):
    """One canonical Markdown packet under a task master's ``requirements/`` root."""

    name: str
    path: str
    address: str
    size: int
    sha256: str


class RequirementsListing(WireResponse):
    """``GET /api/requirements/list``: the registered root selected by task context."""

    repo: str
    master: str
    document: str
    registered: bool
    requirements: list[RequirementRow]


class RequirementContents(WireResponse):
    """``GET /api/requirements/read``: exact UTF-8 bytes decoded from one packet."""

    repo: str
    master: str
    document: str
    name: str
    path: str
    address: str
    size: int
    sha256: str
    content: str


# --- change-set -----------------------------------------------------------------------------


class ChangedFile(WireResponse):
    """One changed file with its per-file counts (``None`` for a binary numstat)."""

    path: str
    insertions: int | None = None
    deletions: int | None = None
    status: str


class ChangedCodeFile(ChangedFile):
    """A changed *code* file, which additionally reports whether a sidecar pairs it."""

    has_sidecar: bool


class ChangeCounters(WireResponse):
    """File count plus summed insertions/deletions for one side of a change-set."""

    files: int
    insertions: int
    deletions: int


class ChangeSetCounters(WireResponse):
    """Both sides' counters, always both present."""

    code: ChangeCounters
    memory: ChangeCounters


class TaskChangeSet(WireResponse):
    """``GET /api/changeset/task`` for an enclosure scope: base -> worktree."""

    scope: str
    code: list[ChangedCodeFile]
    memory: list[ChangedFile]
    counters: ChangeSetCounters


class LeafChangeSet(TaskChangeSet):
    """The same shape for a leaf view, which additionally names the mode it was resolved in."""

    mode: Literal["committed", "working"]


class LeafSummary(WireResponse):
    """One leaf's counter breakdown alongside the master's net diff."""

    leaf_id: str
    counters: ChangeSetCounters


class MasterChangeSet(WireResponse):
    """``GET /api/changeset/master``: the NET series diff, plus the per-leaf breakdown."""

    master: str
    leaves: list[LeafSummary]
    code: list[ChangedCodeFile]
    memory: list[ChangedFile]
    counters: ChangeSetCounters


class DiffSide(WireResponse):
    """One side of a file diff. Absent entirely (``null``) for a pure add or delete."""

    content: str


class FileDiff(WireResponse):
    """``GET /api/changeset/file-diff``: BEFORE + AFTER content, never unified-diff text."""

    scope: str
    kind: Literal["code", "memory"]
    path: str
    language: str
    before: DiffSide | None = None
    after: DiffSide | None = None


# --- harness capability + control -----------------------------------------------------------


class EffortOptionWire(WireResponse):
    """One vendor effort token accepted by one specific model."""

    key: str
    display_name: str
    description: str | None = None
    launch_settable: bool
    session_settable: bool


class ModelCapabilityWire(WireResponse):
    """One advertised model with its model-gated effort menu."""

    key: str
    display_name: str
    resolved_model: str | None = None
    description: str | None = None
    supports_effort: bool
    effort_options: list[EffortOptionWire]
    default_effort: str | None = None
    is_default: bool
    hidden: bool
    selectable: bool
    provider: str | None = None


class ConfigSelectOptionWire(WireResponse):
    """One selectable value of a session config option."""

    value: str
    name: str
    description: str | None = None


class SessionConfigOptionWire(WireResponse):
    """The ACP-shaped select projection of the catalog, category-keyed."""

    type: Literal["select"]
    id: str
    category: str
    name: str
    description: str | None = None
    current_value: str
    options: list[ConfigSelectOptionWire]


class CapabilitySnapshotWire(WireResponse):
    """The normalized capability catalog plus what is currently selected."""

    models: list[ModelCapabilityWire]
    selected_model_key: str | None = None
    selected_effort: str | None = None
    config_options: list[SessionConfigOptionWire]


class HarnessCapabilityEnvelope(WireResponse):
    """``GET /api/harnesses/{harness}/capabilities``: the daemon envelope around the snapshot."""

    schema_version: str = Field(alias="schema")
    harness: str
    cache_status: str
    install_fingerprint: str | None = None
    capabilities: CapabilitySnapshotWire


class SetResultWire(WireResponse):
    """``/set-model`` and ``/set-effort``: honest mutation evidence, never a bare ack."""

    ok: bool
    acceptance: str
    requested_value: str
    effective_value: str | None = None
    detail: str | None = None


class SubmissionAuthorityWire(WireResponse):
    """``/submission-authority``: the one fact every write against it must repeat."""

    bridge_epoch: str


class SubmissionStatusWire(WireResponse):
    """Raw-free public status for one caller-owned cockpit request id."""

    state: str
    submitted_at: str | None = None
    updated_at: str | None = None
    accepted_at: str | None = None
    withdrawable: bool
    detail: str | None = None


class SubmissionLookupFound(WireResponse):
    """The ledger holds this request id."""

    request_id: str
    outcome: Literal["found"]
    submission: SubmissionStatusWire


class SubmissionLookupMissing(WireResponse):
    """The ledger does not hold this request id -- an answer, not a failure."""

    request_id: str
    outcome: Literal["not-found"]


SubmissionLookup = Annotated[
    SubmissionLookupFound | SubmissionLookupMissing, Field(discriminator="outcome")
]


class SubmissionStatusBatchWire(WireResponse):
    """``/submission-status``: one lookup per requested id, under one epoch."""

    bridge_epoch: str
    submissions: list[SubmissionLookup]


class AssetReferenceWire(WireResponse):
    """A recovered asset's wire identity; the runner-local spool path never crosses."""

    asset_id: str
    mime_type: str
    byte_size: int
    sha256: str


class WithdrawalRecoveryWire(WireResponse):
    """The draft a withdrawal handed back, so the composer can restore it."""

    text: str
    assets: list[AssetReferenceWire]


class WithdrawalResultWire(WireResponse):
    """``/withdraw``: the outcome, plus recovery at the one true withdrawal transition."""

    request_id: str
    outcome: Literal["withdrawn", "not-withdrawable", "not-found"]
    state: str
    withdrawn_at: str | None = None
    detail: str | None = None
    recovery: WithdrawalRecoveryWire | None = None


class PublicReceiptWire(WireResponse):
    """``/submit``: normalized submission evidence, without internal vendor diagnostics."""

    request_id: str
    acceptance: str
    submitted_at: str | None = None
    vendor_correlation_id: str | None = None
    accepted_at: str | None = None
    detail: str | None = None
    bridge_epoch: str


class PublicReconciliationWire(WireResponse):
    """``/reconcile``: normalized reconciliation evidence, without vendor diagnostics."""

    request_id: str
    state: str
    reconciled_at: str | None = None
    vendor_correlation_id: str | None = None
    detail: str | None = None
    bridge_epoch: str
    submission_state: str | None = None


class InteractionQuestionOptionWire(WireResponse):
    """One selectable option of a structured interaction question page."""

    label: str
    description: str | None = None


class InteractionQuestionWire(WireResponse):
    """One structured question page of a vendor interaction."""

    text: str
    header: str
    options: list[InteractionQuestionOptionWire]
    multi_select: bool


class PendingInteractionWire(WireResponse):
    """One vendor question still waiting on an answer."""

    interaction_id: str
    kind: str
    prompt: str
    created_at: str
    choices: list[str]
    raw: dict[str, Any]
    questions: list[InteractionQuestionWire]


class InteractionAnswered(WireResponse):
    """``/interaction-response``: the snapshot the answer produced.

    ``pendingInteraction`` stays the parent-thread slot; ``pendingInteractions`` is the
    additive multiplexed sub-agent list. Both are declared because both are emitted.
    """

    status: Literal["accepted"]
    activity: str
    acceptance: str
    pending_interaction: PendingInteractionWire | None = None
    pending_interactions: list[PendingInteractionWire]


# --- shared ``responses=`` tables ------------------------------------------------------------
#
# Declared once because the refusal idiom is shared. A route adds to its table only the
# statuses it can actually produce; nothing here is boilerplate carried by every route.

SCOPED_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": StatusRefusal, "description": "Confinement breach or malformed selector"},
    404: {
        "model": UnknownRepoRefusal | UnknownScopeRefusal | MissingPathRefusal,
        "description": "Unknown repo, unknown scope, or a path that is not there",
    },
}
"""The files / notes / change-set family: ``run_scoped`` and its two siblings map every domain
error onto exactly these two statuses."""

SESSION_CONTROL_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": UnknownSessionRefusal, "description": "No live, bridge-backed seat"},
    409: {
        "model": UnsupportedSeatRefusal | BridgeEpochMismatchRefusal,
        "description": "Seat has no control endpoint, or the caller's epoch is stale",
    },
    503: {"model": StatusRefusal, "description": "The control bridge refused or is unreachable"},
}
"""Every ``harness_control_api`` route: ``_control_route`` resolves the seat, then answers a
control failure through ``_control_failure_response``."""


ACTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ActionTargetRefusal | ActionDetailRefusal, "description": "Ill-posed action"},
    404: {"model": UnknownTargetRefusal, "description": "No such target in the projection"},
    409: {
        "model": ActionTargetRefusal | ActionDetailRefusal,
        "description": "Unavailable, disabled, or decided against a gate that has moved",
    },
    503: {"model": HttpDetailRefusal, "description": "Projection not ready"},
}
"""``/api/actions/{action}``: the evaluator's refusals plus the not-ready projection."""

TerminalCleanupResult.model_rebuild()
