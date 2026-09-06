"""The cold-start guard: the server must import and start with no network egress.

This is a regression guard for a class of defect, not for one library. The class is
"something on the server's import path reaches the network", and its cost is that the
server cannot start where there is none -- a fresh container, an offline machine, a
hermetic CI job. The instance that motivated the guard was ``models/tokens.py`` building
its default counter at module scope, which made ``tiktoken.get_encoding("o200k_base")``
download the vocabulary from openaipublic.blob.core.windows.net while
``mcp/tools/base.py`` was still importing.

The test is a subprocess for two reasons, both of which decide whether it can fail at all:

* tiktoken memoizes loaded encodings in ``tiktoken.registry.ENCODINGS``, and any earlier
  test in the session has already populated it. In-process, the load under test would be
  a dictionary hit that never touches the disk, let alone the network, and the assertion
  would pass against a package that ships no vocabulary at all.
* the caches must be cold. The child gets ``TIKTOKEN_CACHE_DIR``, ``DATA_GYM_CACHE_DIR``,
  ``TMPDIR`` and ``HOME`` pointed at an empty directory, so every place tiktoken looks --
  including the default ``<tmp>/data-gym-cache`` -- is empty. Pointing the operator
  variable at a *cold* directory is deliberate: it also proves the package's own vendored
  copy wins over an exported one that would send the load back to the network.

Nothing here imports ``agents_remember.models.tokens`` at module scope, and nothing may.
That module builds ``DEFAULT_TOKEN_COUNTER`` at import, so a module-scope import runs the
load under test inside the *parent* pytest process, during collection, before any
assertion exists to see what it did -- and the parent has network and a writable checkout,
which is precisely the pair of conditions the guard is asserting the absence of. It cost
this file its teeth once already: with the load happening at collection,
``tiktoken.load.read_file_cached`` met the mismatching bytes first, deleted the vendored
file, re-downloaded it into the package directory and let all eight tests report green.
Everything below imports the module inside the test that needs it, via ``tokens_module``.

The child blocks egress before importing anything, and proves the block took effect before
trusting the run. Verified to bite, on a scratch copy of the tree with the vendored
vocabulary deleted: the child dies while importing the server, with
``TokenizerVocabularyError`` raised from ``DEFAULT_TOKEN_COUNTER = TiktokenTokenCounter()``
in ``models/tokens.py`` -- the refusal, not the ``requests.exceptions.ConnectionError`` an
unguarded download would raise, because ``vendored_vocabulary_cache`` never lets the load
reach the network at all. The probe exits 1 and both ``ColdStartTests`` fail on its return
code with that traceback as the message; the rest of this file fails alongside them,
because ``tokens_module()`` raises the same refusal in this process. Thirteen failures, no
collection error, nothing skipped.

Removal is only the loudest way to break the vendored file. The likelier one is a copy that
is present and wrong, and no child can observe that one -- tiktoken never gets far enough
to try the network. That case is ``CorruptVendoredVocabularyTests``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest import mock

from tiktoken import load as tiktoken_load

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.errors import TokenizerVocabularyError

# tiktoken's own cache variable. Spelled out rather than imported from the module under
# test, because importing that module up here is the defect this file guards against. Its
# siblings -- DATA_GYM_CACHE_DIR, TMPDIR, HOME -- are literals where the probe sets them;
# this one is a constant only because two places need it.
TIKTOKEN_CACHE_DIR_ENV = "TIKTOKEN_CACHE_DIR"

# The repository file that keeps the vendored copy's bytes intact through a checkout, and
# the directory its entry has to name a file in. That entry is a literal path rather than a
# glob, so it is found by the directory it lives in and then checked against the name of
# the file actually shipped.
GITATTRIBUTES = REPO_ROOT / ".gitattributes"
VENDORED_VOCABULARY_ATTRIBUTE_PREFIX = "mcp/src/agents_remember/package_data/tiktoken/"

# How long a re-entrant load may take before the deadlock it is checking for is the only
# remaining explanation. Generous against a cold first tiktoken load (seconds), and finite
# because the failure mode under test is an unbounded wait: a regression must report the
# deadlock, not reproduce it inside the suite.
REENTRANT_LOAD_TIMEOUT_SECONDS = 60.0

# The payload the child counts. Fixed so the parent can count the same thing with its own
# already-loaded counter and compare: equal counts are the proof that the vendored
# vocabulary is the real one and that this change moved no numbers.
PROBE_PAYLOAD = {"ok": True, "operation": "ping"}

PROBE = '''\
"""Start the MCP server with every network path closed, and report what it counted."""

import json
import socket
import sys
import tempfile
from pathlib import Path


class BlockedNetwork(OSError):
    """Raised in place of any outbound connection this process attempts."""


def _blocked(*args, **kwargs):
    raise BlockedNetwork("network egress is blocked by the cold-start guard")


# Patch the connection calls rather than the `socket.socket` class itself: replacing the
# class breaks unrelated imports that subclass it, which turns a clean "no egress" run
# into a confusing failure that proves nothing.
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
socket.gethostbyname = _blocked

# A block that silently did not take would make every assertion below vacuous.
try:
    socket.create_connection(("example.invalid", 80))
except BlockedNetwork:
    pass
else:
    raise SystemExit("the network block did not take effect; the run proves nothing")

from agents_remember.kernel.primitives.checkout_coordination import declare_test_process
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.mcp.server import create_server
from agents_remember.models.tokens import DEFAULT_TOKEN_COUNTER

declare_test_process()
root = Path(tempfile.mkdtemp())
server = create_server(
    McpRuntimeConfig(
        config_path=root / "settings.json",
        coordination_root=root,
        workspace_root=root,
        transcript_root=root / "logs" / "mcp",
    )
)
payload = json.loads(sys.argv[1])
print(
    json.dumps(
        {
            "server": server.name,
            "tokenizer": DEFAULT_TOKEN_COUNTER.name,
            "exact": DEFAULT_TOKEN_COUNTER.exact,
            "tokens": DEFAULT_TOKEN_COUNTER.count(payload),
        }
    )
)
'''


def tokens_module() -> ModuleType:
    """Import the module under test, from inside the test that needs it.

    Deliberately not a module-scope import -- see this module's docstring for what a
    collection-time load costs. ``importlib.import_module`` rather than a function-level
    ``import`` statement because PLC0415 forbids the latter and there is no per-file ignore
    for it; the module object is the same either way.
    """

    return importlib.import_module("agents_remember.models.tokens")


def run_cold_start_probe(workspace: Path) -> subprocess.CompletedProcess[str]:
    """Run the probe in a child with cold tiktoken caches and this checkout on the path."""

    cold = workspace / "cold-cache"
    cold.mkdir()
    script = workspace / "cold_start_probe.py"
    script.write_text(PROBE, encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(MCP_SRC),
            TIKTOKEN_CACHE_DIR_ENV: str(cold),
            "DATA_GYM_CACHE_DIR": str(cold),
            "TMPDIR": str(cold),
            "HOME": str(cold),
        }
    )
    return subprocess.run(
        [sys.executable, str(script), json.dumps(PROBE_PAYLOAD)],
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
        check=False,
    )


class ColdStartTests(unittest.TestCase):
    def test_the_server_starts_with_cold_caches_and_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            result = run_cold_start_probe(Path(workspace))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["server"], "Agents Remember")


class CorruptVendoredVocabularyTests(unittest.TestCase):
    """A vendored file that is present but wrong must be refused, never repaired.

    This is the case the cold-start probe structurally cannot see. tiktoken's
    ``read_file_cached`` answers a SHA-256 mismatch by deleting the cached file and
    downloading a replacement over it, so if the digest check is left to tiktoken every
    corruption below ends as a silent fetch that rewrites the installed package directory
    and a suite that reports green -- measured: CRLF-mangling the vendored file, and
    truncating it to half its bytes, both passed while the file was quietly restored.

    Every case corrupts a *copy* in a temporary directory and points the loader at that.
    The shipped file is the subject of these tests; mutating it would leave the checkout
    damaged whenever one of them failed part-way, and the suite would then be asserting
    against its own debris.
    """

    def assert_corruption_is_refused(self, corrupt: Callable[[bytes], bytes]) -> None:
        """Point the loader at a corrupted copy of the vocabulary and require a refusal."""

        tokens = tokens_module()
        shipped = tokens.vendored_vocabulary_path()
        with tempfile.TemporaryDirectory() as directory:
            corrupt_dir = Path(directory)
            copy = corrupt_dir / shipped.name
            copy.write_bytes(corrupt(shipped.read_bytes()))
            digest = hashlib.sha256(copy.read_bytes()).hexdigest()
            self.assertNotEqual(digest, tokens.VENDORED_VOCABULARY_SHA256)
            with (
                mock.patch.object(tokens, "VENDORED_VOCABULARY_DIR", corrupt_dir),
                self.assertRaises(TokenizerVocabularyError) as refusal,
                tokens.vendored_vocabulary_cache(tokens.VENDORED_ENCODING_NAME),
            ):
                self.fail("a vocabulary whose bytes are wrong must never reach tiktoken")
            # The file, the digest expected and the digest found: the operator's next move
            # is to compare their copy against the one tiktoken asks for, and a message that
            # says only "corrupt" does not say against what.
            message = str(refusal.exception)
            self.assertIn(str(copy), message)
            self.assertIn(tokens.VENDORED_VOCABULARY_SHA256, message)
            self.assertIn(digest, message)
            # Still there. tiktoken was never handed the directory, so nothing deleted the
            # copy and nothing downloaded a replacement over it.
            self.assertTrue(copy.is_file())

    def test_a_counter_will_not_build_on_a_corrupt_copy(self) -> None:
        """The production entry point refuses, and refuses before tiktoken reads anything.

        ``TiktokenTokenCounter()`` is the statement that runs while ``mcp/tools/base.py`` is
        importing, so it is the one that has to fail. ``unreachable`` stands in for
        tiktoken's own fetch and earns its place twice: it is the assertion that no read was
        attempted at all, and it is what stops a regression here from actually downloading
        3.6 MB over the corrupt copy the way the unguarded code does.

        The corruption is a single flipped byte -- same length, same line endings, same
        everything a cheaper check than a digest could look at.
        """

        tokens = tokens_module()
        shipped = tokens.vendored_vocabulary_path()

        def unreachable(blobpath: str) -> bytes:
            raise AssertionError(f"the refusal must come before tiktoken reads {blobpath}")

        with tempfile.TemporaryDirectory() as directory:
            corrupt_dir = Path(directory)
            data = bytearray(shipped.read_bytes())
            data[0] ^= 0x01
            (corrupt_dir / shipped.name).write_bytes(data)
            with (
                mock.patch.object(tokens, "VENDORED_VOCABULARY_DIR", corrupt_dir),
                mock.patch.object(tiktoken_load, "read_file", unreachable),
                self.assertRaises(TokenizerVocabularyError),
            ):
                tokens.TiktokenTokenCounter()


if __name__ == "__main__":
    unittest.main()
