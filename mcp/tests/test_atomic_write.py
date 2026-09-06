"""The one atomic publish: what it guarantees, and the guarantees the copies disagreed about.

Thirteen call sites held their own temp-and-replace before L6 and they disagreed about temp
naming, fsync, directory fsync and failure cleanup. Each of those is asserted here, because
"they all call the same helper now" only means something if the helper is the strong version
rather than the average of the four.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

import pytest
from agents_remember.kernel import atomic_write

pytestmark = pytest.mark.fitness


def _leftovers(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".tmp"))


class AtomicWriteTests(unittest.TestCase):
    def test_it_publishes_the_exact_bytes_and_leaves_no_temp_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state.json"
            atomic_write.atomic_write_text(path, '{"a": 1}\n')
            self.assertEqual(path.read_text(encoding="utf-8"), '{"a": 1}\n')
            self.assertEqual(_leftovers(root), [])

    def test_a_reader_never_sees_a_partial_file_because_the_temp_is_private(self) -> None:
        # The property the fixed-name temps could not offer: what a concurrent reader can
        # observe at the destination is only ever the old file or the whole new one.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state.json"
            atomic_write.atomic_write_text(path, "old\n")
            seen: list[str] = []
            real_replace = os.replace

            def observe(source: object, target: object) -> None:
                seen.append(path.read_text(encoding="utf-8"))
                real_replace(source, target)  # type: ignore[arg-type]

            with mock.patch.object(atomic_write.os, "replace", observe):
                atomic_write.atomic_write_text(path, "new-and-longer\n")
            self.assertEqual(seen, ["old\n"])
            self.assertEqual(path.read_text(encoding="utf-8"), "new-and-longer\n")


class AtomicWriteFailureTests(unittest.TestCase):
    def test_a_failed_replace_removes_the_temp_and_leaves_the_destination_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "occupied"
            path.mkdir()
            (path / "child").write_text("kept\n", encoding="utf-8")

            with self.assertRaises(OSError):
                atomic_write.atomic_write_text(path, "new\n")

            self.assertEqual(_leftovers(root), [])
            self.assertTrue(path.is_dir())
            self.assertEqual((path / "child").read_text(encoding="utf-8"), "kept\n")

    def test_cancellation_between_write_and_replace_also_removes_the_temp(self) -> None:
        # `except Exception` would leak the temp here: a KeyboardInterrupt or an asyncio
        # CancelledError is not an Exception, and both can land in exactly this window.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state.json"
            with (
                mock.patch.object(atomic_write.os, "replace", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                atomic_write.atomic_write_text(path, "new\n")
            self.assertEqual(_leftovers(root), [])
            self.assertFalse(path.exists())


class DirectoryFsyncTests(unittest.TestCase):
    def test_the_directory_entry_is_flushed_so_a_completed_rename_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(atomic_write.os, "fsync", wraps=os.fsync) as fsync:
                atomic_write.atomic_write_text(root / "state.json", "x\n")
            # Once for the temp file's own data, once for the directory holding the rename.
            self.assertEqual(fsync.call_count, 2)


class AtomicReplaceTests(unittest.TestCase):
    def test_a_cross_directory_rename_flushes_both(self) -> None:
        # serving/conversation/control/asset_spool promotes spooled bytes into a different
        # directory; the rename changes two directory entries, so both have to be durable.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool, staged = root / "spool", root / "staged"
            spool.mkdir()
            staged.mkdir()
            (spool / "asset").write_text("bytes\n", encoding="utf-8")
            with mock.patch.object(atomic_write, "_fsync_directory") as _fsync_directory:
                atomic_write.atomic_replace(spool / "asset", staged / "asset-0")
            self.assertEqual(_fsync_directory.call_args_list, [mock.call(staged), mock.call(spool)])


if __name__ == "__main__":
    unittest.main()
