"""Tests for the dashboard terminal host and its PTY clients (slice 6d-1).

Four layers, mirroring the host's one impure seam. The subject spans three modules: the tmux
command layer (``serving.terminal_tmux``), the PTY client (``serving.terminal_pty``) and the
registry that spawns clients against durable tmux sessions (``serving.terminal``).

* **Pure command construction** -- ``build_tmux_command`` / ``tmux_session_name`` have no I/O.
* **Registry** -- a pipe-backed *fake* spawner exercises open/idempotency/replace/close/lookup
  without a real child or tmux.
* **Real PTY** -- a spawner that unwraps the tmux argv and runs the bare harness on a real
  :func:`pty.openpty` master drives write/read/resize/exit against the kernel (no tmux needed).
* **tmux integration** -- one end-to-end test through the real default spawner, skipped when
  ``tmux`` or a tmux-usable terminal capability is unavailable (CI-safe, per the slice plan).
"""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.serving.terminal import (
    TerminalHost,
    TerminalHostSeams,
    TerminalSessionSpec,
)
from agents_remember.serving.terminal_pty import PtyProcess, TerminalSession, spawn_pty
from agents_remember.serving.terminal_tmux import (
    tmux_client_environment,
)

_HAS_TMUX = shutil.which("tmux") is not None
_HAS_CAT = shutil.which("cat") is not None


def _raw_spawn(argv: Sequence[str], cwd: Path) -> PtyProcess:
    """A spawner that drops the tmux wrapper and runs the bare harness on a real PTY.

    Lets the host's write/read/resize methods hit a real ``pty.openpty`` master without needing
    tmux installed -- the kernel PTY is the thing under test here, not the tmux client.
    """
    harness = list(argv[argv.index("--") + 1 :])
    return spawn_pty(harness, cwd)


def _read_until(session: TerminalSession, marker: bytes, timeout: float = 10.0) -> bytes:
    """Accumulate output from one PTY client until ``marker`` appears or ``timeout`` elapses."""
    fd = session.master_fd
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and marker not in buf:
        readable, _, _ = select.select([fd], [], [], 0.2)
        if readable:
            buf.extend(session.read_nonblocking())
    return bytes(buf)


@unittest.skipUnless(_HAS_CAT, "needs `cat` for a real PTY child")
class TerminalHostPtyTests(unittest.TestCase):
    """Drive the host against a real kernel PTY (tmux wrapper stripped by ``_raw_spawn``)."""

    def setUp(self) -> None:
        self.host = TerminalHost(TerminalHostSeams(spawn=_raw_spawn))
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        self.host.shutdown()

    def test_write_then_read_roundtrip(self) -> None:
        session = self.host.open("io", TerminalSessionSpec(cwd=self.tmp, command=("cat",)))
        session.write(b"ar-terminal\n")  # cat echoes the line back through the PTY
        self.assertIn(b"ar-terminal", _read_until(session, b"ar-terminal"))


class TerminalInputReadinessTests(unittest.TestCase):
    def test_a_spawn_that_never_started_leaves_no_pty_fd_behind(self) -> None:
        """A failed spawn must not leak the master fd; the dashboard opens one per attach.

        `finally` closes the slave whatever happens, but the master is the long-lived half
        and only the failure path closes it. A leak here is invisible until the serving
        process runs out of descriptors and every later terminal fails to open.
        """
        opened: list[tuple[int, int]] = []
        real_openpty = os.openpty

        def recording_openpty() -> tuple[int, int]:
            pair = real_openpty()
            opened.append(pair)
            return pair

        with (
            mock.patch("agents_remember.serving.terminal_pty.pty.openpty", recording_openpty),
            mock.patch(
                "agents_remember.serving.terminal_pty.subprocess.Popen",
                side_effect=FileNotFoundError("no such executable"),
            ),
            self.assertRaises(FileNotFoundError),
        ):
            spawn_pty(("definitely-not-an-executable",), Path.cwd())

        self.assertEqual(len(opened), 1)
        for fd in opened[0]:
            with self.assertRaises(OSError):
                os.fstat(fd)


@unittest.skipUnless(
    _HAS_TMUX,
    "needs tmux for the end-to-end persistence path",
)
class TerminalHostTmuxIntegrationTests(unittest.TestCase):
    """The full default spawner: a real tmux-wrapped PTY running a marker-printing child."""

    def setUp(self) -> None:
        self.host = TerminalHost()  # real spawn_pty -> real tmux
        self.tmp = Path(tempfile.mkdtemp())
        self.tmux_name: str | None = None

    def tearDown(self) -> None:
        self.host.shutdown()
        if self.tmux_name is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["tmux", "kill-session", "-t", self.tmux_name],
                    check=False,
                    capture_output=True,
                    timeout=5,
                    env=tmux_client_environment(os.environ),
                )

    def test_real_tmux_ensure_and_attach_ignore_launcher_identity(self) -> None:
        contaminated = {
            "TMUX": "/tmp/tmux-1000/default,295199,20",
            "TMUX_PANE": "%21",
            "TERM": "dumb",
        }
        with mock.patch.dict(os.environ, contaminated, clear=False):
            binding = self.host.ensure(
                "tmux-it",
                TerminalSessionSpec(
                    cwd=self.tmp,
                    command=("sh", "-c", "printf AR_READY_MARKER; sleep 10"),
                    lifecycle_id="LC-tmux",
                ),
            )
            session = self.host.attach(
                "tmux-it",
                TerminalSessionSpec(
                    cwd=self.tmp,
                    command=("sh", "-c", "printf AR_READY_MARKER; sleep 10"),
                    lifecycle_id="LC-tmux",
                    name=binding.tmux_name,
                ),
            )
        self.tmux_name = binding.tmux_name
        try:
            self.assertIn(b"AR_READY_MARKER", _read_until(session, b"AR_READY_MARKER"))
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
