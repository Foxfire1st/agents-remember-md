"""Credential-free real-or-explicit-fake proof of the Codex read-only app-server seam."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.kernel.harnesses import Harness
from agents_remember.kernel.platform_subprocess import windows_interop_reason
from agents_remember.serving.codex_app_server_protocol import JsonObject
from agents_remember.serving.conversation.library.codex import probe_app_server_version

CODEX = Harness(id="codex", name="Codex", command="codex", argv=("codex",))


class _FakeReadOnlyTransport:
    """Explicit absence seam; it accepts only initialize/initialized/thread-list."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    async def start(self, launch: object) -> None:
        self.launch = launch

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        before_write: object = None,
    ) -> JsonObject:
        del params, before_write
        self.methods.append(method)
        if method == "initialize":
            return {
                "userAgent": ("agents_remember/0.147.0 (clean-room fake) (agents_remember; 3.0.0)"),
                "codexHome": "/tmp/codex-fake",
                "platformFamily": "unix",
                "platformOs": "linux",
            }
        if method == "thread/list":
            return {"data": [], "nextCursor": None}
        raise AssertionError(f"unexpected mutating or unsupported Codex call: {method}")

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        del params
        self.methods.append(method)

    def messages(self) -> AsyncIterator[JsonObject]:
        return self._no_messages()

    async def _no_messages(self) -> AsyncIterator[JsonObject]:
        if False:
            yield {}

    async def respond(self, request_id: object, result: Mapping[str, object]) -> None:
        del request_id, result

    async def respond_error(self, request_id: object, *, code: int, message: str) -> None:
        del request_id, code, message

    async def stop(self, mode: str) -> None:
        del mode


def _selected_mode() -> tuple[str, str | None]:
    requested = os.environ.get("AR_CODEX_PROBE_MODE", "fake")
    if requested not in {"auto", "real", "fake"}:
        raise AssertionError(f"unknown AR_CODEX_PROBE_MODE: {requested}")
    resolved = shutil.which("codex")
    native = resolved if resolved is not None and windows_interop_reason(resolved) is None else None
    if requested == "real":
        assert native is not None, "real Codex probe selected but no native codex executable exists"
        return "real", native
    if requested == "fake":
        return "fake", None
    return ("real", native) if native is not None else ("fake", None)


def test_codex_initialize_and_thread_list_use_selected_transport() -> None:
    mode, executable = _selected_mode()
    fake = _FakeReadOnlyTransport()
    if mode == "real":
        version = asyncio.run(
            probe_app_server_version(
                CODEX,
                workspace_root=Path.cwd(),
                env=os.environ,
                resolve_executable=lambda _command: executable,
            )
        )
        methods = ["initialize", "initialized", "thread/list"]
    else:
        version = asyncio.run(
            probe_app_server_version(
                CODEX,
                workspace_root=Path.cwd(),
                env=os.environ,
                transport_factory=lambda: fake,
                resolve_executable=lambda _command: sys.executable,
            )
        )
        methods = fake.methods
    assert methods == ["initialize", "initialized", "thread/list"]
    evidence = {
        "mode": mode,
        "codexVersion": version,
        "methods": methods,
        "promptSubmitted": False,
        "threadCreated": False,
    }
    if report := os.environ.get("AR_CODEX_PROBE_REPORT"):
        atomic_write_text(Path(report), json.dumps(evidence, indent=2) + "\n")


def test_codex_probe_defaults_to_fake_without_explicit_gate_four_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AR_CODEX_PROBE_MODE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _command: "/usr/bin/codex")
    monkeypatch.setattr(sys.modules[__name__], "windows_interop_reason", lambda _executable: None)

    assert _selected_mode() == ("fake", None)


def test_codex_probe_real_mode_requires_an_explicit_native_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AR_CODEX_PROBE_MODE", "real")
    monkeypatch.setattr(shutil, "which", lambda _command: "/usr/bin/codex")
    monkeypatch.setattr(sys.modules[__name__], "windows_interop_reason", lambda _executable: None)

    assert _selected_mode() == ("real", "/usr/bin/codex")
