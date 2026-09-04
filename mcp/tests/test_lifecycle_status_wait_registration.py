"""CCR-R15 wait tool registration: the closure delegates to the payload builder.

The worktree_status_wait @server.tool closure in
mcp/registration/worktrees.py must hand its flat public arguments to the
application payload builder as one typed LifecycleStatusWaitRequest and
return the builder's result untouched.  This mirrors the family-wide
registration wiring pattern self-contained, without importing any pre-existing
mcp/tests support module.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.application.lifecycle.lifecycle_status_wait import (
    LifecycleStatusWaitRequest,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.mcp.server import create_server
from agents_remember.models.lifecycles.operation_kinds import LifecycleOperationKind

SENTINEL: dict[str, object] = {"ok": True, "marker": "payload-result"}


class _Recorder:
    """Stands in for the payload builder and remembers the one call it received."""

    def __init__(self) -> None:
        self.args: tuple[object, ...] = ()
        self.kwargs: dict[str, object] = {}
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.args = args
        self.kwargs = kwargs
        self.calls += 1
        return SENTINEL


def _settings_payload(root: Path) -> dict[str, object]:
    return {
        "version": 1,
        "coordinationRoot": (root / "ar-coordination").as_posix(),
        "workspaceRoot": (root / "workspace").as_posix(),
        "repositories": {"agents-remember": {}},
        "providers": {
            "codegraphcontext-code": {},
            "grepai-memory": {},
        },
        "timeoutCaps": {
            "toolSeconds": 30,
            "providerSetupSeconds": 1800,
        },
        "benchmarksEnabled": True,
    }


class LifecycleStatusWaitRegistrationTests(unittest.TestCase):
    """One live server per test with the wait payload builder recorded."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        settings_path = root / "mcp-settings.json"
        settings_path.write_text(
            json.dumps(_settings_payload(root), indent=2),
            encoding="utf-8",
        )
        self.config = load_config(settings_path)
        self.server = create_server(self.config)

    def test_wait_tool_delegates_one_typed_request_to_the_payload_builder(self) -> None:
        recorder = _Recorder()
        target = "agents_remember.mcp.registration.worktrees.worktree_status_wait_payload"
        with mock.patch(target, recorder):
            _content, structured = asyncio.run(
                self.server.call_tool(
                    "worktree_status_wait",
                    {
                        "contract_path": "/tasks/example/series-contract.md",
                        "operation_kind": "closeout",
                        "expected_generation": 1,
                        "after_revision": 2,
                        "timeout_seconds": 1.5,
                    },
                )
            )
        self.assertEqual(recorder.calls, 1)
        self.assertEqual(structured, SENTINEL)
        self.assertGreaterEqual(len(recorder.args), 2)
        request = recorder.args[1]
        self.assertIsInstance(request, LifecycleStatusWaitRequest)
        assert isinstance(request, LifecycleStatusWaitRequest)
        self.assertEqual(request.contract_path, "/tasks/example/series-contract.md")
        self.assertEqual(request.operation_kind, "closeout")
        self.assertIsInstance(request.operation_kind, str)
        kind: LifecycleOperationKind = request.operation_kind
        self.assertIn(kind, ("closeout", "integrate", "direct-landing"))
        self.assertEqual(request.expected_generation, 1)
        self.assertEqual(request.after_revision, 2)
        self.assertEqual(request.timeout_seconds, 1.5)

    def test_wait_tool_is_advertised_between_status_and_sync(self) -> None:
        tools = asyncio.run(self.server.list_tools())
        names = [tool.name for tool in tools]
        self.assertIn("worktree_status_wait", names)
        self.assertLess(names.index("worktree_status"), names.index("worktree_status_wait"))
        self.assertLess(names.index("worktree_status_wait"), names.index("worktree_sync"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
