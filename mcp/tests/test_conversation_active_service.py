"""Projector engine: hydration, ordering, idempotence, provenance, rehydration, gaps."""

import unittest
from collections.abc import Mapping

from agents_remember.errors import (
    HarnessBridgeEpochMismatchError,
)
from agents_remember.models.conversations.control_wire import (
    AdapterSnapshot,
    ControlIdentity,
    SubmissionProvenance,
    SubmissionProvenanceBatch,
)
from agents_remember.models.conversations.evidence import (
    EvidenceFrame,
    EvidencePage,
    NativeEvidenceFrame,
    NativeEvidencePage,
)
from agents_remember.models.conversations.identity import (
    ActiveConversationRef,
    AuthorizationBinding,
)
from agents_remember.serving.conversation.active.projector import (
    ActiveSessionProjector,
    EvidenceTimelineRegressed,
)
from agents_remember.serving.conversation.active.projector.facade import ProjectedSession
from agents_remember.serving.conversation.active.projector.wiring import BridgeReaders
from agents_remember.serving.conversation.projectors import projector_for

NOW = "2026-07-19T08:00:00+00:00"
SECRET = b"x" * 32


def _identity(harness: str = "codex") -> ActiveConversationRef:
    return ActiveConversationRef(
        harness_id=harness,  # type: ignore[arg-type]
        vendor_conversation_id="vendor-1",
        project_scope="/workspace",
        identity_digest="digest-1",
        ar_session_id="ar-1",
        bridge_epoch="epoch-1",
    )


def _authorization() -> AuthorizationBinding:
    return AuthorizationBinding(principal_id="local-operator:1000", tenant_id="/workspace")


class _ControlledEntry:
    id = "ar-1"
    tmux_name = "ar-t-1"
    created_at = NOW
    control_endpoint = None


class _ScriptedBridge:
    """In-memory scripted substrate mirroring the IPC read shapes."""

    def __init__(self, *, harness: str = "codex", epoch: str = "epoch-1") -> None:
        self.epoch = epoch
        self.harness = harness
        self.evidence_frames: list[EvidenceFrame] = []
        self.transcript_entries: list[Mapping[str, object]] = []
        self.native_frames: list[NativeEvidenceFrame] = []
        self.native_error: Exception | None = None
        self.provenance: dict[str, str] = {}
        self.evicted_before = 0
        self.snapshot = AdapterSnapshot(
            identity=ControlIdentity(ar_session_id="ar-1", tmux_name="ar-t-1", created_at=NOW),
            control="ready",
            activity="idle",
            acceptance="immediate",
            vendor_session_id="vendor-1",
            raw={},
        )

    def push_evidence(self, kind: str, raw: dict) -> None:
        self.evidence_frames.append(
            EvidenceFrame(
                sequence=len(self.evidence_frames) + 1,
                kind=kind,
                created_at=NOW,
                raw=raw,
            )
        )

    def read_evidence(self, entry, *, after_sequence=0, limit=500, expected_bridge_epoch=None):
        del entry
        if expected_bridge_epoch is not None and expected_bridge_epoch != self.epoch:
            raise HarnessBridgeEpochMismatchError(expected_bridge_epoch, self.epoch)
        frames = [f for f in self.evidence_frames if f.sequence > after_sequence]
        latest = self.evidence_frames[-1].sequence if self.evidence_frames else 0
        return EvidencePage(
            frames=tuple(frames[:limit]),
            latest_sequence=latest,
            evicted_before_sequence=self.evicted_before,
            truncated=len(frames) > limit,
            bridge_epoch=self.epoch,
        )

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_conversation_active_service.py:107).
    def read_native_page(
        self, entry, *, cursor=None, limit=200, expected_bridge_epoch=None
    ):  # pragma: no cover
        # The production seam (``read_control_native_page``) takes exactly these; a double that
        # also accepted a ``byte_budget`` would let a caller pass one the real reader rejects.
        del entry, expected_bridge_epoch
        if self.native_error is not None:
            raise self.native_error
        start = 0
        if cursor is not None:
            for index, frame in enumerate(self.native_frames):
                if frame.native_id == cursor:
                    start = index + 1
                    break
        selected = self.native_frames[start : start + limit]
        truncated = start + limit < len(self.native_frames)
        return NativeEvidencePage(
            frames=tuple(selected),
            next_cursor=selected[-1].native_id if truncated and selected else None,
            truncated=truncated,
            bridge_epoch=self.epoch,
        )

    def read_transcript(self, entry, *, after_sequence=0, limit=500):
        del entry
        return tuple(
            entry_ for entry_ in self.transcript_entries if _entry_sequence(entry_) > after_sequence
        )[:limit]

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_conversation_active_service.py:134).
    def read_provenance(self, entry, *, expected_bridge_epoch, request_ids):  # pragma: no cover
        del entry
        if expected_bridge_epoch != self.epoch:
            raise HarnessBridgeEpochMismatchError(expected_bridge_epoch, self.epoch)
        return SubmissionProvenanceBatch(
            bridge_epoch=self.epoch,
            provenance=tuple(
                SubmissionProvenance(
                    request_id=request_id,
                    outcome="found" if request_id in self.provenance else "not-found",
                    source=self.provenance.get(request_id),  # type: ignore[arg-type]
                    state="delivered",
                    submitted_at=NOW,
                    updated_at=NOW,
                    accepted_at=NOW,
                )
                for request_id in request_ids
            ),
        )

    def read_snapshot(self, entry) -> AdapterSnapshot:
        del entry
        return self.snapshot


def _entry_sequence(entry: Mapping[str, object]) -> int:
    value = entry.get("sequence")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _projector(bridge: _ScriptedBridge, harness: str = "codex") -> ActiveSessionProjector:
    mapper = projector_for(harness)
    assert mapper is not None
    return ActiveSessionProjector(
        ProjectedSession(
            identity=_identity(harness),
            authorization=_authorization(),
            entry=_ControlledEntry(),  # type: ignore[arg-type]
            # type: ignore[arg-type]
            mapper=mapper,
            secret=SECRET,
        ),
        clock=lambda: NOW,
        readers=BridgeReaders(
            evidence=bridge.read_evidence,
            native_page=bridge.read_native_page,
            transcript=bridge.read_transcript,
            provenance=bridge.read_provenance,
            snapshot=bridge.read_snapshot,
        ),
    )


def _codex_turn(bridge: _ScriptedBridge, turn: str, *, prompt: str = "go") -> None:
    bridge.push_evidence(
        "codex-notification",
        {
            "threadId": "vendor-1",
            "turnId": turn,
            "startedAtMs": 1,
            "item": {
                "id": f"{turn}-user",
                "type": "userMessage",
                "clientId": f"req-{turn}",
                "content": [{"type": "text", "text": prompt}],
            },
        },
    )
    bridge.push_evidence(
        "codex-notification",
        {
            "threadId": "vendor-1",
            "turnId": turn,
            "startedAtMs": 2,
            "item": {"id": f"{turn}-agent", "type": "agentMessage", "text": "working"},
        },
    )
    bridge.push_evidence(
        "codex-notification",
        {"threadId": "vendor-1", "turnId": turn, "itemId": f"{turn}-agent", "delta": " more"},
    )
    bridge.push_evidence(
        "completed",
        {"threadId": "vendor-1", "turn": {"id": turn, "status": "completed", "items": []}},
    )


class CodexEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_hydration_orders_items_and_mints_stable_ids(self) -> None:
        bridge = _ScriptedBridge()
        _codex_turn(bridge, "turn-1")
        projector = _projector(bridge)
        result = await projector.page(before_ordinal=None, limit=50)
        ids = [item.item_id for item in result.items]
        self.assertEqual(
            ids,
            ["turn-1-user", "turn-1-agent", "turn-result:turn-1"],
        )
        agent = result.items[1]
        from agents_remember.models.conversations.content import MarkdownBlock  # noqa: PLC0415

        self.assertIsInstance(agent.blocks[0], MarkdownBlock)
        self.assertEqual(
            next(block for block in agent.blocks if isinstance(block, MarkdownBlock)).markdown,
            "working more",
        )
        self.assertEqual(agent.revision, 2)
        self.assertEqual([item.global_ordinal for item in result.items], [1, 2, 3])
        self.assertEqual(result.total_items, 3)
        self.assertFalse(result.has_older)

    async def test_settled_live_turns_project_once_when_native_ids_disjoint(self) -> None:
        """A settled codex turn must project EXACTLY once.

        On a hosted codex ``+ Chat`` thread the live notification channel and ``thread/read`` use
        DISJOINT id namespaces for the same settled turn (live ``msg_*``/UUID item ids vs positional
        ``item-N``), so the store's id-keyed dedupe can never converge them and
        ``_refresh_native_tip``'s post-turn native re-walk mints a second, ``native-history`` twin of
        every settled turn (developer-visible duplicate rows, deterministic 2/2 turns on the wire).
        Here the native page groups the twins under the SAME turn ids the live channel used.
        """
        bridge = _ScriptedBridge()
        _codex_turn(bridge, "turn-1")
        projector = _projector(bridge)
        await projector.page(before_ordinal=None, limit=50)
        _codex_turn(bridge, "turn-2")
        await projector.poll_once()  # both turns now settled live under their live-id namespace
        live = await projector.page(before_ordinal=None, limit=50)
        live_ids = [item.item_id for item in live.items]
        self.assertEqual(
            live_ids,
            [
                "turn-1-user",
                "turn-1-agent",
                "turn-result:turn-1",
                "turn-2-user",
                "turn-2-agent",
                "turn-result:turn-2",
            ],
        )
        # The hosted thread now persists the SAME settled turns through thread/read under DISJOINT
        # positional item ids, grouped under the live turn ids.
        bridge.native_frames.extend(
            [
                NativeEvidenceFrame(
                    native_id="item-1",
                    native_parent_id="turn-1",
                    native_type="userMessage",
                    created_at=NOW,
                    raw={
                        "id": "item-1",
                        "type": "userMessage",
                        "clientId": "req-turn-1",
                        "content": [{"type": "text", "text": "go"}],
                    },
                ),
                NativeEvidenceFrame(
                    native_id="item-2",
                    native_parent_id="turn-1",
                    native_type="agentMessage",
                    created_at=NOW,
                    raw={"id": "item-2", "type": "agentMessage", "text": "working more"},
                ),
                NativeEvidenceFrame(
                    native_id="item-3",
                    native_parent_id="turn-2",
                    native_type="userMessage",
                    created_at=NOW,
                    raw={
                        "id": "item-3",
                        "type": "userMessage",
                        "clientId": "req-turn-2",
                        "content": [{"type": "text", "text": "go"}],
                    },
                ),
                NativeEvidenceFrame(
                    native_id="item-4",
                    native_parent_id="turn-2",
                    native_type="agentMessage",
                    created_at=NOW,
                    raw={"id": "item-4", "type": "agentMessage", "text": "working more"},
                ),
            ]
        )
        # Any further live evidence marks the native tip dirty; the next page re-walks thread/read.
        bridge.push_evidence(
            "codex-notification",
            {"threadId": "vendor-1", "turnId": "turn-2", "tokenUsage": {"totalTokens": 1}},
        )
        await projector.poll_once()
        settled = await projector.page(before_ordinal=None, limit=50)
        settled_ids = [item.item_id for item in settled.items]
        self.assertEqual(
            settled_ids, live_ids, "settled turns must project once, never as native-history twins"
        )
        self.assertNotIn("item-1", settled_ids)
        self.assertEqual(settled.total_items, len(live_ids))

    async def test_prior_session_native_history_survives_live_turns(self) -> None:
        """The suppression must never drop GENUINE prior-session history.

        A resumed thread's earlier turns (never observed live in this projector) must hydrate in full;
        only turns this run settled live are suppressed from the native re-walk.
        """
        bridge = _ScriptedBridge()
        bridge.native_frames.append(
            NativeEvidenceFrame(
                native_id="hist-1",
                native_parent_id="turn-0",
                native_type="agentMessage",
                created_at=NOW,
                raw={"id": "hist-1", "type": "agentMessage", "text": "earlier session"},
            )
        )
        _codex_turn(bridge, "turn-1")
        projector = _projector(bridge)
        result = await projector.page(before_ordinal=None, limit=50)
        ids = [item.item_id for item in result.items]
        self.assertEqual(ids, ["hist-1", "turn-1-user", "turn-1-agent", "turn-result:turn-1"])

    async def test_native_page_hydration_and_older_paging(self) -> None:
        bridge = _ScriptedBridge()
        for index in range(5):
            bridge.native_frames.append(
                NativeEvidenceFrame(
                    native_id=f"native-{index}",
                    native_parent_id="turn-0",
                    native_type="agentMessage",
                    created_at=NOW,
                    raw={"id": f"native-{index}", "type": "agentMessage", "text": f"n{index}"},
                )
            )
        _codex_turn(bridge, "turn-1")
        projector = _projector(bridge)
        latest = await projector.page(before_ordinal=None, limit=3)
        self.assertEqual(
            [item.item_id for item in latest.items],
            ["turn-1-user", "turn-1-agent", "turn-result:turn-1"],
        )
        self.assertTrue(latest.has_older)
        older = await projector.page(before_ordinal=6, limit=3)
        self.assertEqual(
            [item.item_id for item in older.items], ["native-2", "native-3", "native-4"]
        )
        self.assertTrue(older.has_older)
        oldest = await projector.page(before_ordinal=3, limit=3)
        self.assertEqual([item.item_id for item in oldest.items], ["native-0", "native-1"])
        self.assertFalse(oldest.has_older)
        self.assertEqual(oldest.total_items, 8)

    async def test_gap_on_epoch_flip(self) -> None:
        bridge = _ScriptedBridge()
        _codex_turn(bridge, "turn-1")
        projector = _projector(bridge)
        await projector.page(before_ordinal=None, limit=50)
        bridge.epoch = "epoch-2"
        with self.assertRaises(HarnessBridgeEpochMismatchError):
            await projector.poll_once()


class ClaudeEngineTests(unittest.IsolatedAsyncioTestCase):
    def _claude_turn(self, bridge: _ScriptedBridge, n: int) -> None:
        bridge.transcript_entries.append(
            {
                "sequence": n * 2 - 1,
                "role": "user",
                "text": f"prompt {n}",
                "createdAt": NOW,
                "requestId": f"req-{n}",
                "vendorCorrelationId": f"uuid-user-{n}",
            }
        )
        bridge.push_evidence(
            "state",
            {
                "type": "assistant",
                "uuid": f"uuid-agent-{n}",
                "session_id": "vendor-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"answer {n}"}],
                },
            },
        )
        bridge.push_evidence(
            "completed",
            {"type": "result", "subtype": "success", "is_error": False, "uuid": f"uuid-result-{n}"},
        )

    async def test_zipper_handles_split_result_across_polls(self) -> None:
        bridge = _ScriptedBridge(harness="claude")
        # Turn 1 result arrives only in a later poll than user 2's echo.
        bridge.transcript_entries.append(
            {
                "sequence": 1,
                "role": "user",
                "text": "p1",
                "createdAt": NOW,
                "requestId": "req-1",
                "vendorCorrelationId": "uuid-user-1",
            }
        )
        bridge.transcript_entries.append(
            {
                "sequence": 2,
                "role": "user",
                "text": "p2",
                "createdAt": NOW,
                "requestId": "req-2",
                "vendorCorrelationId": "uuid-user-2",
            }
        )
        bridge.push_evidence(
            "state",
            {
                "type": "assistant",
                "uuid": "uuid-agent-1",
                "session_id": "vendor-1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
            },
        )
        projector = _projector(bridge, harness="claude")
        bridge2 = bridge
        result = await projector.page(before_ordinal=None, limit=50)
        self.assertEqual(
            [item.item_id for item in result.items],
            ["uuid-user-1", "uuid-agent-1", "uuid-user-2"],
        )
        bridge2.push_evidence(
            "completed",
            {"type": "result", "subtype": "success", "is_error": False, "uuid": "uuid-result-1"},
        )
        bridge2.transcript_entries.append(
            {
                "sequence": 3,
                "role": "user",
                "text": "p3",
                "createdAt": NOW,
                "requestId": "req-3",
                "vendorCorrelationId": "uuid-user-3",
            }
        )
        await projector.poll_once()
        result2 = await projector.page(before_ordinal=None, limit=50)
        ids = [item.item_id for item in result2.items]
        self.assertLess(ids.index("uuid-result-1:result"), ids.index("uuid-user-3"))

    async def test_page_never_claims_completeness_while_frames_pend(self) -> None:
        # Pending zipper frames are projected content the store cannot show
        # yet. A page that claims a complete totalItems while frames pend tells the client
        # "this is the whole history" — which the UI honestly renders as a short transcript
        # over a live session. Completeness must account for the pend.
        #
        # The pend must NOT be signalled through hasOlder. Pending frames are NEWER
        # content and the older-history channel cannot carry them: widening hasOlder alone
        # leaves olderOrdinal None, `exclude_none` drops olderCursor from the wire, and the
        # browser renders a live "Load older" button whose click refetches the newest page
        # forever — sticky for the life of the projection, on a page that simultaneously
        # reported a complete total. hasOlder stays correlated with its cursor; the total goes
        # unknown instead.
        bridge = _ScriptedBridge(harness="claude")
        # An assistant frame with NO user echo yet: it lands in _pending_frames for its turn.
        bridge.push_evidence(
            "state",
            {
                "type": "assistant",
                "uuid": "uuid-agent-orphan",
                "session_id": "vendor-1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            },
        )
        projector = _projector(bridge, harness="claude")
        settled = await projector.page(before_ordinal=None, limit=50)
        self.assertEqual(
            settled.total_items,
            1,
            "with nothing pending the window is complete and its total is known",
        )
        # Force the pend to persist: a fresh frame with its echo still absent.
        projector._echo.pending_frames.append(
            EvidenceFrame(
                sequence=99,
                kind="state",
                created_at=NOW,
                raw={
                    "type": "assistant",
                    "uuid": "uuid-agent-pending",
                    "session_id": "vendor-1",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
                },
            )
        )
        pended = await projector.page(before_ordinal=None, limit=50)
        self.assertIsNone(
            pended.total_items,
            "a page must not claim a complete total while zipper frames are still pending",
        )
        self.assertFalse(
            pended.has_older,
            "pending frames are NEWER content: they must never be signalled as older history",
        )
        self.assertEqual(
            pended.has_older,
            pended.older_ordinal is not None,
            "hasOlder must hold exactly when an older cursor exists (store.py mints both "
            "from one expression); a claimed older page with no cursor is a dead button",
        )

    async def test_regressed_evidence_tip_gaps_instead_of_freezing(self) -> None:
        # `caught_up` compares the consumed watermark against the bridge's
        # latest_sequence. If the bridge re-baselines its evidence numbering (tip moves
        # BACKWARD), the comparison is true forever and the projector silently freezes on its
        # pre-reset window while the session keeps running. A regressed tip must fail loud.
        bridge = _ScriptedBridge(harness="claude")
        self._claude_turn(bridge, 1)
        projector = _projector(bridge, harness="claude")
        await projector.page(before_ordinal=None, limit=50)
        # The bridge re-baselines: its whole evidence buffer resets behind the consumed tip.
        bridge.evidence_frames.clear()
        with self.assertRaises(EvidenceTimelineRegressed):
            await projector.poll_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
