from __future__ import annotations

from pathlib import Path
from unittest import mock

from agents_remember.controlplane.expectation_rows import ExpectationRowStore
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
)
from agents_remember.mcp.tools.operator_inbox import operator_inbox_post_payload
from agents_remember.models.conversations.control_wire import (
    SubmissionReceipt,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.observer import observer_root
from agents_remember.serving.conversation.ports import TerminalCatalogPort
from agents_remember.serving.dispatch_brief import (
    DispatchBriefGate,
    HostedDelivery,
)
from agents_remember.serving.harness_control_models import (
    ReconciliationResult,
)
from agents_remember.serving.hosted_readiness import HostedReadinessResult
from agents_remember.serving.hosted_session_runtime import HostedSessionRuntime
from agents_remember.serving.inbox_delivery import (
    DeliveryAdmission,
    InboxDeliveryLog,
    deliver_inbox_entry,
)
from agents_remember.serving.terminal import TerminalHost, TerminalHostSeams
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)
from agents_remember.serving.terminal_paste import PasteResult

NOW = "2026-07-14T10:00:00+00:00"


def _config(root: Path) -> McpRuntimeConfig:
    return McpRuntimeConfig(
        config_path=root / "settings.json",
        coordination_root=root,
        workspace_root=root,
        transcript_root=root / "logs" / "mcp",
    )


def _target(root: Path, session_id: str = "worker-1") -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label="Worker",
        kind="harness",
        harness="codex",
        lifecycle_id="LC-worker",
        cwd=root,
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at=NOW,
        last_attached_at=NOW,
        status="running",
        task_document_ref=TaskDocumentRef(repository="repo", path="master/leaf-1.json"),
        seat_role="worker",
        spawn_role="worker",
        prompt_keywords=("ultracode",),
        control_state="ready",
        control_endpoint=root / f"{session_id}.sock",
        control_protocol="ar-harness-control/v1",
    )


class _NoRawPaster:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def paste(self, tmux_name: str, text: str, *, submit: bool = False, **_kwargs: object):
        self.calls.append((tmux_name, text, submit))
        return PasteResult(delivered=True, submitted=submit)


def _ready(entry: TerminalCatalogEntry):
    def check(_catalog: TerminalCatalogPort, _host: object, session_id: str):
        assert session_id == entry.id
        return HostedReadinessResult("ready", session_id, entry=entry)

    return check


def _post(
    root: Path,
    *,
    entry: TerminalCatalogEntry,
    catalog: TerminalCatalog,
    acceptance: str = "immediate",
) -> tuple[dict[str, object], mock.Mock, _NoRawPaster]:
    paster = _NoRawPaster()

    def submit(_target: object, _text: str, submission: object) -> SubmissionReceipt:
        return SubmissionReceipt(
            request_id=str(submission.request_id),  # type: ignore[attr-defined]
            acceptance=acceptance,  # type: ignore[arg-type]
            submitted_at=NOW,
            accepted_at=NOW if acceptance == "immediate" else None,
        )

    with mock.patch(
        "agents_remember.serving.inbox_delivery.submit_control_prompt", side_effect=submit
    ) as submit_prompt:
        posted = operator_inbox_post_payload(
            _config(root),
            address=InboxAddress(lifecycle_id=None, agent_id=entry.id, recipient_role="worker"),
            message=InboxMessage(
                ask="Implement the leaf",
                response="Use the exact task contract.",
                message_kind="dispatch-brief",
            ),
            poster=InboxPoster(
                created_by="manager-1",
                created_via="cli",
                sender_agent_id="manager-1",
                sender_role="manager",
            ),
            delivery=HostedDelivery(
                enabled=True,
                catalog=catalog,
                host=TerminalHost(
                    TerminalHostSeams(tmux_probe=lambda name: name == entry.tmux_name)
                ),
                paster=paster,  # type: ignore[arg-type]
                readiness=_ready(entry),
                gate=DispatchBriefGate(readiness=_ready(entry)),
            ),
        )
    return posted, submit_prompt, paster


def test_ready_dispatch_is_inbox_rooted_lands_and_starts_expectation_clocks(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    catalog = TerminalCatalog(tmp_path / "logs" / "dashboard" / "terminal-sessions.json")
    target = _target(tmp_path)
    catalog.upsert(target)
    posted, submit_prompt, paster = _post(tmp_path, entry=target, catalog=catalog)

    assert posted["deliveryState"] == "delivered"
    assert posted["adapterDeliveryState"] == "accepted"
    # N16: a brief accepted at a ready (turn-boundary) seat is terminal ``landed``.
    assert posted["state"] == "landed"
    assert posted["agentId"] == target.id
    assert paster.calls == []
    assert submit_prompt.call_count == 1
    assert "ultracode" in submit_prompt.call_args.args[1]
    assert str(posted["entryId"]) not in submit_prompt.call_args.args[1]

    durable = OperatorInboxStore(observer_root(config)).current()[str(posted["entryId"])]
    assert durable.state == "landed"
    expectations = ExpectationRowStore(observer_root(config)).current()
    by_kind = {row.kind: row for row in expectations.values()}
    # ack-by is retired with the N16 consume demotion; only the briefed-by clock rides here.
    assert set(by_kind) == {"briefed-by"}
    assert by_kind["briefed-by"].state == "met"


def test_rejected_adapter_receipt_keeps_same_row_pending(tmp_path: Path) -> None:
    catalog = TerminalCatalog(tmp_path / "logs" / "dashboard" / "terminal-sessions.json")
    target = _target(tmp_path)
    catalog.upsert(target)
    posted, _submit, paster = _post(tmp_path, entry=target, catalog=catalog, acceptance="rejected")
    assert posted["deliveryState"] == "unconfirmed"
    assert posted["adapterDeliveryState"] == "rejected"
    assert posted["state"] == "pending"
    assert paster.calls == []


def test_ambiguous_redelivery_reconciles_without_resubmitting(tmp_path: Path) -> None:
    catalog = TerminalCatalog(tmp_path / "terminal-sessions.json")
    target = _target(tmp_path)
    catalog.upsert(target)
    store = OperatorInboxStore(tmp_path / "observer")
    entry = create_operator_inbox_entry(
        InboxMessage(ask="Brief", response="Work", message_kind="dispatch-brief"),
        entry_id="dispatch-1",
        now=NOW,
        routing=InboxRouting(
            address=InboxAddress(lifecycle_id=target.lifecycle_id, agent_id=target.id)
        ),
        poster=InboxPoster(created_by="manager", created_via="cli"),
    ).model_copy(
        update={
            "adapterRequestId": "dispatch-1",
            "adapterDeliveryState": "unknown",
            "deliveryState": "unconfirmed",
        }
    )
    store.append(entry)
    paster = _NoRawPaster()
    with (
        mock.patch(
            "agents_remember.serving.inbox_delivery.reconcile_control_prompt",
            return_value=ReconciliationResult(
                request_id="dispatch-1",
                state="accepted",
                reconciled_at=NOW,
            ),
        ),
        mock.patch("agents_remember.serving.inbox_delivery.submit_control_prompt") as submit_prompt,
    ):
        delivered = deliver_inbox_entry(
            InboxDeliveryLog(store=store, entry=entry),
            sessions=HostedSessionRuntime(
                catalog=catalog, host=TerminalHost(TerminalHostSeams(tmux_probe=lambda _name: True))
            ),
            paster=paster,  # type: ignore[arg-type]
            admission=DeliveryAdmission(dispatch_gate=DispatchBriefGate(readiness=_ready(target))),
        )
    assert delivered.deliveryState == "delivered"
    assert delivered.adapterDeliveryState == "accepted"
    # The reconciled acceptance at a ready boundary lands the row (N16).
    assert delivered.state == "landed"
    submit_prompt.assert_not_called()
    assert paster.calls == []


def test_not_ready_queues_one_durable_dispatch_row_for_plane_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    catalog = TerminalCatalog(tmp_path / "terminal-sessions.json")
    target = _target(tmp_path)
    catalog.upsert(target)

    def not_ready(_catalog: TerminalCatalogPort, _host: object, session_id: str):
        return HostedReadinessResult("not-ready", session_id, entry=target, detail="starting")

    posted = operator_inbox_post_payload(
        config,
        address=InboxAddress(lifecycle_id=None, agent_id=target.id),
        message=InboxMessage(ask="Brief", response="Work", message_kind="dispatch-brief"),
        poster=InboxPoster(created_by="manager-1", created_via="cli"),
        delivery=HostedDelivery(
            catalog=catalog,
            host=TerminalHost(TerminalHostSeams(tmux_probe=lambda _name: True)),
            paster=_NoRawPaster(),  # type: ignore[arg-type]
            readiness=not_ready,
        ),
    )
    assert posted["state"] == "pending"
    assert posted["deliveryState"] == "unconfirmed"
    assert "not-ready" in str(posted["deliveryDetail"])
    assert len(OperatorInboxStore(observer_root(config)).current()) == 1


def test_exact_agent_target_never_falls_back_to_matching_lifecycle(tmp_path: Path) -> None:
    catalog = TerminalCatalog(tmp_path / "terminal-sessions.json")
    target = _target(tmp_path)
    catalog.upsert(target)
    store = OperatorInboxStore(tmp_path / "observer")
    entry = create_operator_inbox_entry(
        InboxMessage(ask="Brief", response="Work", message_kind="dispatch-brief"),
        entry_id="dispatch-1",
        now=NOW,
        routing=InboxRouting(
            address=InboxAddress(lifecycle_id=target.lifecycle_id, agent_id="missing-session")
        ),
        poster=InboxPoster(created_by="manager", created_via="cli"),
    )
    store.append(entry)
    delivered = deliver_inbox_entry(
        InboxDeliveryLog(store=store, entry=entry),
        sessions=HostedSessionRuntime(
            catalog=catalog, host=TerminalHost(TerminalHostSeams(tmux_probe=lambda _name: True))
        ),
        paster=_NoRawPaster(),  # type: ignore[arg-type]
        admission=DeliveryAdmission(dispatch_gate=DispatchBriefGate(readiness=_ready(target))),
    )
    assert delivered.deliveryState == "no-hosted-session"
    assert delivered.deliveredToSession is None
