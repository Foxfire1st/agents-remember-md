from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from unittest import mock

from agents_remember.memory_quality.style.citations import (
    source_index,
    source_index_database,
    source_index_state,
)
from agents_remember.memory_quality.style.citations.resolution import Trees
from test_memory_citation_source_index import MCP_SRC, IndexCase


class PublicationAndBoundsTests1(IndexCase):
    def test_corrupt_database_and_obsolete_manifest_rebuild(self) -> None:
        self.write("one.py", "alpha = 1\n")
        _cold, snapshot = self.acquire()
        paths = source_index.cache_paths(self.trees)
        paths.database.write_bytes(b"not sqlite")
        rebuilt, same_snapshot = self.acquire()
        self.assertEqual(rebuilt["state"], "built")
        self.assertEqual(same_snapshot, snapshot)
        manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
        manifest["schemaVersion"] = -1
        paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        obsolete, final_snapshot = self.acquire()
        self.assertEqual(obsolete["state"], "built")
        self.assertEqual(final_snapshot, snapshot)

    def test_cache_is_external_fixed_slot_and_per_file_cap_fails_closed(self) -> None:
        self.write("large.txt", "12345")
        paths = source_index.cache_paths(self.trees)
        self.assertNotIn(self.code.resolve(), paths.root.parents)
        self.assertNotIn(self.memory.resolve(), paths.root.parents)
        self.assertLess(int(paths.slot.name.removeprefix("slot-")), source_index.CACHE_SLOT_COUNT)
        with (
            mock.patch.object(source_index_state, "MAX_SOURCE_FILE_BYTES", 4),
            self.assertRaises(source_index.SourceIndexError),
        ):
            source_index.open_repository_index(self.trees)
        self.assertFalse(paths.database.exists())
        self.assertFalse(paths.manifest.exists())
        self.assertFalse(paths.readiness.exists())

    def test_repository_slot_collision_revalidates_identity(self) -> None:
        self.write("one.py", "alpha = 1\n")
        with mock.patch.object(source_index, "CACHE_SLOT_COUNT", 1):
            _first, first_snapshot = self.acquire()
            other_code = self.root / "other-code"
            other_memory = self.root / "other-memory"
            other_code.mkdir()
            other_memory.mkdir()
            (other_code / "two.py").write_text("beta = 2\n", encoding="utf-8")
            with source_index.open_repository_index(Trees(other_code, other_memory)) as index:
                self.assertEqual(index.metrics.state, "built")
                self.assertNotEqual(index.snapshot_id, first_snapshot)
            rebuilt, restored_snapshot = self.acquire()
        self.assertEqual(rebuilt["state"], "built")
        self.assertEqual(restored_snapshot, first_snapshot)

    def test_cache_lifecycle_is_bounded_at_two_slot_and_repository_sizes(self) -> None:
        for slot_count, repository_count in ((1, 3), (2, 6)):
            with self.subTest(slot_count=slot_count, repository_count=repository_count):
                cache = self.root / f"cache-{slot_count}"
                with (
                    mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}),
                    mock.patch.object(source_index, "CACHE_SLOT_COUNT", slot_count),
                ):
                    parent = source_index.cache_root().parent
                    for version in (1, 3, 5):
                        legacy = parent / f"citation-source-index-v{version}" / "slot-0"
                        legacy.mkdir(parents=True)
                        (legacy / "index.lock").write_bytes(b"")
                        (legacy / "index.sqlite3").write_bytes(b"obsolete")
                    for repository in range(repository_count):
                        code = self.root / f"code-{slot_count}-{repository}"
                        memory = self.root / f"memory-{slot_count}-{repository}"
                        code.mkdir()
                        memory.mkdir()
                        (code / "one.py").write_text(
                            f"symbol_{repository} = {repository}\n", encoding="utf-8"
                        )
                        with source_index.open_repository_index(Trees(code, memory)) as index:
                            self.assertEqual(index.metrics.state, "built")
                    self.assertEqual(
                        [
                            path.name
                            for path in parent.iterdir()
                            if source_index.LEGACY_CACHE_NAME.fullmatch(path.name)
                        ],
                        [],
                    )
                    slots = list(source_index.cache_root().glob("slot-*"))
                    self.assertLessEqual(len(slots), slot_count)
                    published = sum(
                        path.stat().st_size
                        for slot in slots
                        for path in (
                            slot / "index.sqlite3",
                            slot / "manifest.json",
                            slot / "ready.json",
                        )
                        if path.exists()
                    )
                    self.assertLessEqual(published, slot_count * source_index.MAX_INDEX_BYTES)

    def test_concurrent_legacy_reclamation_publishes_only_stable_slots(self) -> None:
        parent = source_index.cache_root().parent
        for version in range(1, 6):
            for slot_number in range(2):
                slot = parent / f"citation-source-index-v{version}" / f"slot-{slot_number}"
                slot.mkdir(parents=True)
                (slot / "index.lock").write_bytes(b"")
                (slot / "index.sqlite3").write_bytes(b"obsolete")
        repositories: list[Trees] = []
        for repository in range(4):
            code = self.root / f"concurrent-code-{repository}"
            memory = self.root / f"concurrent-memory-{repository}"
            code.mkdir()
            memory.mkdir()
            (code / "one.py").write_text(f"symbol_{repository} = 1\n", encoding="utf-8")
            repositories.append(Trees(code, memory))
        start = self.root / "start-reclamation"
        script = """
import json, sys, time
from pathlib import Path
from agents_remember.memory_quality.style.citations import source_index
from agents_remember.memory_quality.style.citations.resolution import Trees
start=Path(sys.argv[3])
deadline=time.monotonic()+10
while not start.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
with source_index.open_repository_index(Trees(Path(sys.argv[1]), Path(sys.argv[2]))) as index:
    print(json.dumps({'state': index.metrics.state, 'snapshot': index.snapshot_id}))
"""
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(one.code_root),
                    str(one.memory_root),
                    str(start),
                ],
                env={**os.environ, "PYTHONPATH": str(MCP_SRC), "XDG_CACHE_HOME": str(self.cache)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for one in repositories
        ]
        start.write_text("go\n", encoding="utf-8")
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["state"], "built")

        self.assertFalse(
            any(source_index.LEGACY_CACHE_NAME.fullmatch(path.name) for path in parent.iterdir())
        )
        self.assertLessEqual(
            len(list(source_index.cache_root().glob("slot-*"))), source_index.CACHE_SLOT_COUNT
        )

    def test_live_legacy_slots_are_not_deleted_and_share_one_bounded_wait(self) -> None:
        self.write("one.py", "alpha = 1\n")
        parent = source_index.cache_root().parent
        first = parent / "citation-source-index-v4" / "slot-0"
        second = parent / "citation-source-index-v5" / "slot-0"
        handles = []
        for legacy in (first, second):
            legacy.mkdir(parents=True)
            handle = (legacy / "index.lock").open("a+b")
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            handles.append(handle)

        def release_first() -> None:
            fcntl.flock(handles[0].fileno(), fcntl.LOCK_UN)

        release = threading.Timer(0.04, release_first)
        release.start()
        started = time.monotonic()
        try:
            with (
                mock.patch.object(source_index, "RECLAMATION_LOCK_TIMEOUT_SECONDS", 0.08),
                self.assertRaisesRegex(source_index.SourceIndexError, "timed out"),
            ):
                source_index.open_repository_index(self.trees)
            self.assertLess(time.monotonic() - started, 0.11)
            self.assertTrue(second.parent.exists())
        finally:
            release.join()
            for handle in handles:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

        built, _snapshot = self.acquire()
        self.assertEqual(built["state"], "built")
        self.assertFalse(first.parent.exists())
        self.assertFalse(second.parent.exists())

    def test_anchor_digest_collision_is_rejected(self) -> None:
        self.write("one.py", "alpha = 1\nbeta = 2\n")
        with (
            mock.patch.object(source_index_database, "_anchor_key", return_value=b"same"),
            self.assertRaises(source_index_database.SourceIndexDatabaseError),
        ):
            source_index.open_repository_index(self.trees)

    def test_stale_builder_temp_is_reclaimed_by_next_publisher(self) -> None:
        self.write("one.py", "alpha = 1\n")
        paths = source_index.cache_paths(self.trees)
        paths.slot.mkdir(parents=True, exist_ok=True)
        stale = paths.slot / ".index.sqlite3.999.dead.tmp"
        stale.write_text("partial", encoding="utf-8")

        built, _snapshot = self.acquire()

        self.assertEqual(built["state"], "built")
        self.assertFalse(stale.exists())
