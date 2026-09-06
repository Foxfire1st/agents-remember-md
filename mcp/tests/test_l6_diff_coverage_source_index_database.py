"""L6 closeout coverage tests for citation source-index database branches."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.memory_quality.style.citations import extents
from agents_remember.memory_quality.style.citations import source_index_database as db
from agents_remember.memory_quality.style.citations.source_index_database import (
    Database,
    SourceIndexDatabaseError,
    _digest_value,
    _metadata_integer,
    _pack_posting,
    _unpack_extents,
    _unpack_marks,
    _validate_generation_metadata,
    _validate_short_postings,
)
from agents_remember.memory_quality.style.citations.source_index_state import ReadyGeneration


def _ready() -> ReadyGeneration:
    return ReadyGeneration(
        generation_id="a" * 64,
        snapshot_id="b" * 64,
        code_root="/code",
        memory_root="/memory",
        files_indexed=1,
        source_bytes=10,
        database_bytes=100,
    )


def _metadata(**over: str) -> dict[str, str]:
    base = {
        "schema_version": str(db.source_index_state.SCHEMA_VERSION),
        "readiness_state": "ready",
        "generation_id": "a" * 64,
        "snapshot_id": "b" * 64,
        "code_root": "/code",
        "memory_root": "/memory",
        "candidate_tree": "",
        "files_indexed": "1",
        "source_bytes": "10",
        "quote_stream_count": "0",
        "quote_text_bytes": "0",
        "application_sha256": hashlib.sha256(b"x").hexdigest(),
    }
    base.update(over)
    return base


class TestMetadataValidation:
    def test_metadata_integer_errors(self) -> None:
        with pytest.raises(SourceIndexDatabaseError, match="invalid"):
            _metadata_integer({"x": "abc"}, "x", minimum=0, maximum=10)
        with pytest.raises(SourceIndexDatabaseError, match="invalid"):
            _metadata_integer({"x": "5"}, "x", minimum=0, maximum=3)

    def test_generation_metadata_errors(self) -> None:
        ready = _ready()
        _validate_generation_metadata(_metadata(), ready)
        with pytest.raises(SourceIndexDatabaseError, match="metadata is malformed"):
            _validate_generation_metadata({}, ready)
        missing_candidate = _metadata()
        del missing_candidate["candidate_tree"]
        with pytest.raises(SourceIndexDatabaseError, match="metadata is malformed"):
            _validate_generation_metadata(missing_candidate, ready)
        with pytest.raises(SourceIndexDatabaseError, match="schema is obsolete"):
            _validate_generation_metadata(_metadata(schema_version="0"), ready)
        with pytest.raises(SourceIndexDatabaseError, match="is not ready"):
            _validate_generation_metadata(_metadata(readiness_state="no"), ready)
        with pytest.raises(SourceIndexDatabaseError, match="identity do not match"):
            _validate_generation_metadata(_metadata(generation_id="c" * 64), ready)
        with pytest.raises(SourceIndexDatabaseError, match="different source roots"):
            _validate_generation_metadata(_metadata(code_root="/other"), ready)
        with pytest.raises(SourceIndexDatabaseError, match="different source roots or candidate"):
            _validate_generation_metadata(_metadata(candidate_tree="c" * 40), ready)
        with pytest.raises(SourceIndexDatabaseError, match="application digest is malformed"):
            _validate_generation_metadata(_metadata(application_sha256="zzz"), ready)


class TestDatabaseBranches:
    def _db(self, connection: object) -> Database:
        return Database(
            cast(sqlite3.Connection, connection),
            quote_stream_count=0,
            quote_text_bytes=0,
            files_indexed=0,
            source_bytes=0,
        )

    def test_insert_file_no_id(self) -> None:
        connection = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(lastrowid=None))
        with pytest.raises(SourceIndexDatabaseError, match="file id was not assigned"):
            self._db(connection).insert_file("a.py", ["x = 1\n"])

    def test_bytes_used_missing_metadata(self) -> None:
        connection = SimpleNamespace(execute=lambda sql, *a: SimpleNamespace(fetchone=lambda: None))
        with pytest.raises(SourceIndexDatabaseError, match="size metadata is absent"):
            self._db(connection).bytes_used()

    def test_paths_for_missing_file(self) -> None:
        connection = SimpleNamespace(
            execute=lambda sql, *args: [(1, "a.py")] if "SELECT file_id, path" in sql else []
        )
        with pytest.raises(SourceIndexDatabaseError, match="references a missing file"):
            self._db(connection)._paths_for((1, 2))

    def test_flush_postings_threshold(self) -> None:
        connection = SimpleNamespace(
            execute=lambda *a, **k: SimpleNamespace(lastrowid=1),
            executemany=lambda *a, **k: None,
            commit=lambda: None,
        )
        database = self._db(connection)
        with mock.patch.object(db, "_POSTING_BUFFER_KEYS", 0):
            database._buffer_postings(
                1,
                [("symbol", "f", (extents.Extent(1, 1, extents.DEFINITION),))],
            )
        assert not database.posting_buffer


class TestPackUnpack:
    def test_pack_posting_too_many(self) -> None:
        spans = tuple(extents.Extent(i, i, extents.DEFINITION) for i in range(0x10000))
        with pytest.raises(SourceIndexDatabaseError, match="too many extents"):
            _pack_posting(1, spans)

    def test_unpack_extents_errors(self) -> None:
        with pytest.raises(SourceIndexDatabaseError, match="extent posting is corrupt"):
            _unpack_extents(b"abc")
        with pytest.raises(SourceIndexDatabaseError, match="extent kind is corrupt"):
            _unpack_extents(struct.pack("<IIB", 1, 2, 99))

    def test_digest_value_unsupported(self) -> None:
        digest = hashlib.sha256()
        with pytest.raises(SourceIndexDatabaseError, match="unsupported"):
            _digest_value(digest, 1.5)

    def test_unpack_marks_errors(self) -> None:
        with pytest.raises(SourceIndexDatabaseError, match="word map is corrupt"):
            _unpack_marks("x", b"not-zlib")
        raw = struct.pack("<IIII", 5, 1, 1, 0)
        with pytest.raises(SourceIndexDatabaseError, match="word map is corrupt"):
            _unpack_marks("abc", zlib.compress(raw))

    def test_validate_short_postings(self) -> None:
        stream_ids = {1}
        raw = struct.pack("<I", 1)
        rows = [(b"g", raw)]
        connection = SimpleNamespace(execute=lambda *a: rows)
        typed = cast(sqlite3.Connection, connection)
        _validate_short_postings(typed, stream_ids, {b"g": bytearray(raw)})
        with pytest.raises(SourceIndexDatabaseError, match="missing stream"):
            _validate_short_postings(typed, {2}, {b"g": bytearray(raw)})
        with pytest.raises(SourceIndexDatabaseError, match="corrupt"):
            _validate_short_postings(typed, stream_ids, {b"g": bytearray(b"xx")})
        with pytest.raises(SourceIndexDatabaseError, match="corrupt"):
            _validate_short_postings(typed, stream_ids, {b"other": bytearray(raw)})
