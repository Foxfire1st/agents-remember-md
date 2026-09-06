"""SQLite storage for one immutable citation source-index generation."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from agents_remember.memory_quality.style.citations import (
    extents,
    grammars,
    model,
    source_index_state,
)
from agents_remember.memory_quality.style.document_shape import inline_scan

_MARK = struct.Struct("<IIII")
_EXTENT_RECORD = struct.Struct("<IIB")
_POSTING_HEADER = struct.Struct("<IH")
_STREAM_ID = struct.Struct("<I")
_POSTING_BUFFER_BYTES = 4 * 1024 * 1024
_POSTING_BUFFER_KEYS = 64 * 1024
_SQLITE_PARAMETER_BATCH = 250
_EXTENT_CODE = {
    extents.DEFINITION: 0,
    extents.OCCURRENCE: 1,
    extents.SECTION: 2,
}
_EXTENT_KIND = {code: kind for kind, code in _EXTENT_CODE.items()}


class SourceIndexDatabaseError(ValueError):
    """An index database is incompatible, corrupt, or mismatched."""


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None: ...


def _metadata_integer(metadata: dict[str, str], key: str, *, minimum: int, maximum: int) -> int:
    raw = metadata.get(key)
    if raw is None or not raw.isascii() or not raw.isdecimal():
        raise SourceIndexDatabaseError(
            f"citation source-index database has invalid {key.replace('_', ' ')}"
        )
    value = int(raw)
    if not minimum <= value <= maximum or str(value) != raw:
        raise SourceIndexDatabaseError(
            f"citation source-index database has invalid {key.replace('_', ' ')}"
        )
    return value


def _validate_generation_metadata(
    metadata: dict[str, str], readiness: source_index_state.ReadyGeneration
) -> None:
    if set(metadata) != {
        "schema_version",
        "readiness_state",
        "generation_id",
        "snapshot_id",
        "code_root",
        "memory_root",
        "candidate_tree",
        "files_indexed",
        "source_bytes",
        "quote_stream_count",
        "quote_text_bytes",
        "application_sha256",
    }:
        raise SourceIndexDatabaseError("citation source-index database metadata is malformed")
    if metadata.get("schema_version") != str(source_index_state.SCHEMA_VERSION):
        raise SourceIndexDatabaseError("citation source-index database schema is obsolete")
    if metadata.get("readiness_state") != "ready":
        raise SourceIndexDatabaseError("citation source-index database is not ready")
    if (
        metadata.get("generation_id") != readiness.generation_id
        or metadata.get("snapshot_id") != readiness.snapshot_id
    ):
        raise SourceIndexDatabaseError(
            "citation source-index readiness and database identity do not match"
        )
    if (
        metadata.get("code_root") != readiness.code_root
        or metadata.get("memory_root") != readiness.memory_root
        or metadata.get("candidate_tree") != (readiness.candidate_tree or "")
    ):
        raise SourceIndexDatabaseError(
            "citation source-index database belongs to different source roots or candidate"
        )
    if not source_index_state.canonical_hash(metadata.get("application_sha256")):
        raise SourceIndexDatabaseError(
            "citation source-index database application digest is malformed"
        )


def _generation_counters(
    metadata: dict[str, str], readiness: source_index_state.ReadyGeneration
) -> tuple[int, int, int, int]:
    files_indexed = _metadata_integer(
        metadata, "files_indexed", minimum=0, maximum=source_index_state.MAX_SOURCE_FILES
    )
    source_bytes = _metadata_integer(
        metadata, "source_bytes", minimum=0, maximum=source_index_state.MAX_SOURCE_BYTES
    )
    if files_indexed != readiness.files_indexed or source_bytes != readiness.source_bytes:
        raise SourceIndexDatabaseError(
            "citation source-index readiness and database counters do not match"
        )
    quote_stream_count = _metadata_integer(
        metadata,
        "quote_stream_count",
        minimum=0,
        maximum=source_index_state.MAX_DATABASE_BYTES,
    )
    quote_text_bytes = _metadata_integer(
        metadata,
        "quote_text_bytes",
        minimum=0,
        maximum=source_index_state.MAX_DATABASE_BYTES,
    )
    return files_indexed, source_bytes, quote_stream_count, quote_text_bytes


@dataclass(frozen=True)
class IndexedFile:
    """All extents one anchor has in one indexed file."""

    path: str
    extents: tuple[extents.Extent, ...]


@dataclass
class Database:
    """Typed access to one SQLite source-index generation."""

    connection: sqlite3.Connection
    direct_anchor_queries: int = 0
    quote_anchor_queries: int = 0
    quote_index_lookups: int = 0
    quote_short_gram_lookups: int = 0
    quote_candidate_streams_read: int = 0
    quote_candidate_text_bytes_read: int = 0
    quote_stream_count: int = 0
    quote_text_bytes: int = 0
    files_indexed: int = 0
    source_bytes: int = 0
    posting_buffer: dict[bytes, bytearray] = field(default_factory=dict)
    posting_buffer_bytes: int = 0
    short_quote_buffer: dict[bytes, bytearray] = field(default_factory=dict)
    short_quote_buffer_bytes: int = 0

    @classmethod
    def create(cls, path: Path) -> Database:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = FILE;
            PRAGMA cache_size = -32768;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (
                file_id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE
            );
            CREATE TABLE anchor_names (
                anchor_key BLOB PRIMARY KEY,
                anchor_kind TEXT NOT NULL,
                anchor_text TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TRIGGER reject_anchor_key_collision
            BEFORE INSERT ON anchor_names
            WHEN EXISTS (
                SELECT 1 FROM anchor_names
                WHERE anchor_key = NEW.anchor_key
                  AND (anchor_kind != NEW.anchor_kind OR anchor_text != NEW.anchor_text)
            )
            BEGIN
                SELECT RAISE(ABORT, 'citation anchor key collision');
            END;
            CREATE TABLE direct_postings (
                anchor_key BLOB PRIMARY KEY,
                postings BLOB NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE quote_streams (
                stream_id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                text BLOB NOT NULL,
                marks BLOB NOT NULL,
                UNIQUE(file_id, ordinal)
            );
            CREATE VIRTUAL TABLE quote_search USING fts5(
                text,
                content='',
                tokenize='trigram case_sensitive 1',
                detail='none'
            );
            CREATE VIRTUAL TABLE quote_vocab USING fts5vocab(quote_search, 'row');
            CREATE VIRTUAL TABLE quote_instances USING fts5vocab(quote_search, 'instance');
            CREATE TABLE quote_short_postings (
                gram BLOB PRIMARY KEY,
                stream_ids BLOB NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE call_literals (
                file_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                start INTEGER NOT NULL,
                end INTEGER NOT NULL,
                argument_start_byte INTEGER NOT NULL,
                argument_end_byte INTEGER NOT NULL,
                PRIMARY KEY(file_id, ordinal)
            ) WITHOUT ROWID;
            """
        )
        return cls(connection)

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        readiness: source_index_state.ReadyGeneration,
        verify_integrity: bool,
    ) -> Database:
        if path.stat().st_size != readiness.database_bytes:
            raise SourceIndexDatabaseError(
                "citation source-index readiness and database size do not match"
            )
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            _validate_generation_metadata(metadata, readiness)
            files_indexed, source_bytes, quote_stream_count, quote_text_bytes = (
                _generation_counters(metadata, readiness)
            )
            if verify_integrity:
                checked = connection.execute("PRAGMA quick_check").fetchone()
                if checked is None or str(checked[0]) != "ok":
                    raise SourceIndexDatabaseError("citation source-index database is corrupt")
            database = cls(
                connection,
                quote_stream_count=quote_stream_count,
                quote_text_bytes=quote_text_bytes,
                files_indexed=files_indexed,
                source_bytes=source_bytes,
            )
            if verify_integrity:
                database.validate_application_integrity()
        except sqlite3.DatabaseError as error:
            connection.close()
            raise SourceIndexDatabaseError("citation source-index database is corrupt") from error
        except BaseException:
            connection.close()
            raise
        return database

    def close(self) -> None:
        self.connection.close()

    def insert_file(self, path: str, lines: list[str]) -> None:
        """Derive and persist all exact-query structures for one source file."""
        inserted = self.connection.execute("INSERT INTO files(path) VALUES (?)", (path,))
        file_id = inserted.lastrowid
        if file_id is None:
            raise SourceIndexDatabaseError("citation source-index file id was not assigned")
        defined = extents.definitions(path, lines)
        occurrences: dict[str, list[int]] = {}
        for line_number, line in enumerate(lines, start=1):
            for name in set(model.IDENTIFIER_PATTERN.findall(line)):
                occurrences.setdefault(name, []).append(line_number)
        direct: list[tuple[str, str, tuple[extents.Extent, ...]]] = []
        for name in sorted(occurrences.keys() | defined.keys()):
            spans = tuple(defined.get(name, ())) or _occurrence_extents(occurrences.get(name, []))
            direct.append((model.SYMBOL, name, spans))
        levels = extents.heading_levels(inline_scan.unfenced_lines(lines))
        for heading in sorted({lines[index].strip() for index in levels}):
            direct.append(
                (
                    model.HEADING,
                    heading,
                    extents.heading_extents_in(heading, lines, levels),
                )
            )
        names = [(_anchor_key(kind, text), kind, text) for kind, text, _spans in direct]
        try:
            self.connection.executemany(
                "INSERT OR IGNORE INTO anchor_names VALUES (?, ?, ?)", names
            )
        except sqlite3.IntegrityError as error:
            raise SourceIndexDatabaseError(str(error)) from error
        self._buffer_postings(file_id, direct)
        streams = (extents.collapsed(lines), *extents.line_comment_blocks(lines))
        self.quote_stream_count += len(streams)
        self.quote_text_bytes += sum(len(stream.text.encode("utf-8")) for stream in streams)
        for ordinal, stream in enumerate(streams):
            inserted_stream = self.connection.execute(
                "INSERT INTO quote_streams(file_id, ordinal, text, marks) VALUES (?, ?, ?, ?)",
                (file_id, ordinal, _pack_text(stream.text), _pack_marks(stream.marks)),
            )
            stream_id = inserted_stream.lastrowid
            if stream_id is None:
                raise SourceIndexDatabaseError(
                    "citation source-index quote stream id was not assigned"
                )
            self.connection.execute(
                "INSERT INTO quote_search(rowid, text) VALUES (?, ?)",
                (stream_id, stream.text),
            )
            self._buffer_short_grams(stream_id, stream.text)
        self.connection.executemany(
            "INSERT INTO call_literals VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    file_id,
                    ordinal,
                    one.text,
                    one.start,
                    one.end,
                    one.argument_start_byte,
                    one.argument_end_byte,
                )
                for ordinal, one in enumerate(grammars.call_argument_literals(path, lines))
            ],
        )

    def write_snapshot(self, readiness: source_index_state.ReadyGeneration) -> None:
        self._flush_postings()
        self._flush_short_grams()
        application_sha256 = _application_digest(self.connection)
        self.connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("schema_version", str(source_index_state.SCHEMA_VERSION)),
                ("readiness_state", "ready"),
                ("generation_id", readiness.generation_id),
                ("snapshot_id", readiness.snapshot_id),
                ("code_root", readiness.code_root),
                ("memory_root", readiness.memory_root),
                ("candidate_tree", readiness.candidate_tree or ""),
                ("files_indexed", str(readiness.files_indexed)),
                ("source_bytes", str(readiness.source_bytes)),
                ("quote_stream_count", str(self.quote_stream_count)),
                ("quote_text_bytes", str(self.quote_text_bytes)),
                ("application_sha256", application_sha256),
            ),
        )
        self.connection.commit()

    def bytes_used(self) -> int:
        page_count = self.connection.execute("PRAGMA page_count").fetchone()
        page_size = self.connection.execute("PRAGMA page_size").fetchone()
        if page_count is None or page_size is None:
            raise SourceIndexDatabaseError("citation source-index size metadata is absent")
        return (
            int(page_count[0]) * int(page_size[0])
            + self.posting_buffer_bytes
            + self.short_quote_buffer_bytes
        )

    def _buffer_postings(
        self,
        file_id: int,
        direct: list[tuple[str, str, tuple[extents.Extent, ...]]],
    ) -> None:
        for kind, text, spans in direct:
            posting = _pack_posting(file_id, spans)
            self.posting_buffer.setdefault(_anchor_key(kind, text), bytearray()).extend(posting)
            self.posting_buffer_bytes += len(posting)
        if (
            self.posting_buffer_bytes >= _POSTING_BUFFER_BYTES
            or len(self.posting_buffer) >= _POSTING_BUFFER_KEYS
        ):
            self._flush_postings()

    def _flush_postings(self) -> None:
        if not self.posting_buffer:
            return
        self.connection.executemany(
            "INSERT INTO direct_postings VALUES (?, ?) "
            "ON CONFLICT(anchor_key) DO UPDATE SET "
            "postings = CAST(direct_postings.postings || excluded.postings AS BLOB)",
            self.posting_buffer.items(),
        )
        self.posting_buffer.clear()
        self.posting_buffer_bytes = 0

    def _buffer_short_grams(self, stream_id: int, text: str) -> None:
        grams = set(text) | {text[index : index + 2] for index in range(len(text) - 1)}
        encoded_id = _STREAM_ID.pack(stream_id)
        for gram in grams:
            encoded = gram.encode("utf-8")
            self.short_quote_buffer.setdefault(encoded, bytearray()).extend(encoded_id)
            self.short_quote_buffer_bytes += len(encoded_id)
        if (
            self.posting_buffer_bytes + self.short_quote_buffer_bytes >= _POSTING_BUFFER_BYTES
            or len(self.short_quote_buffer) >= _POSTING_BUFFER_KEYS
        ):
            self._flush_postings()
            self._flush_short_grams()

    def _flush_short_grams(self) -> None:
        if not self.short_quote_buffer:
            return
        self.connection.executemany(
            "INSERT INTO quote_short_postings VALUES (?, ?) "
            "ON CONFLICT(gram) DO UPDATE SET "
            "stream_ids = CAST(quote_short_postings.stream_ids || excluded.stream_ids AS BLOB)",
            self.short_quote_buffer.items(),
        )
        self.short_quote_buffer.clear()
        self.short_quote_buffer_bytes = 0

    def _paths_for(self, file_ids: tuple[int, ...]) -> dict[int, str]:
        """Fetch only paths named by one or more keyed posting streams."""
        requested = tuple(dict.fromkeys(file_ids))
        found: dict[int, str] = {}
        for offset in range(0, len(requested), _SQLITE_PARAMETER_BATCH):
            batch = requested[offset : offset + _SQLITE_PARAMETER_BATCH]
            parameters = ", ".join("?" for _file_id in batch)
            found.update(
                (int(file_id), str(path))
                for file_id, path in self.connection.execute(
                    f"SELECT file_id, path FROM files WHERE file_id IN ({parameters})", batch
                )
            )
        if found.keys() != set(requested):
            raise SourceIndexDatabaseError(
                "citation source-index posting references a missing file"
            )
        return found

    def locations(
        self, anchors: tuple[model.Anchor, ...]
    ) -> dict[model.Anchor, tuple[IndexedFile, ...]]:
        direct = tuple(anchor for anchor in anchors if anchor.kind != model.QUOTE)
        found = self._direct(direct)
        found.update(
            self._quotes(tuple(anchor for anchor in anchors if anchor.kind == model.QUOTE))
        )
        return found

    def _direct(
        self, anchors: tuple[model.Anchor, ...]
    ) -> dict[model.Anchor, tuple[IndexedFile, ...]]:
        self.direct_anchor_queries += len(anchors)
        found: dict[model.Anchor, tuple[IndexedFile, ...]] = {}
        decoded: dict[model.Anchor, tuple[tuple[int, tuple[extents.Extent, ...]], ...]] = {}
        for anchor in anchors:
            key = _anchor_key(anchor.kind, anchor.text)
            named = self.connection.execute(
                "SELECT anchor_kind, anchor_text FROM anchor_names WHERE anchor_key = ?",
                (key,),
            ).fetchone()
            if named is not None and (str(named[0]), str(named[1])) != (
                anchor.kind,
                anchor.text,
            ):
                raise SourceIndexDatabaseError("citation anchor key collision")
            row = self.connection.execute(
                "SELECT CAST(postings AS BLOB) FROM direct_postings WHERE anchor_key = ?",
                (key,),
            ).fetchone()
            decoded[anchor] = () if row is None else _unpack_postings(bytes(row[0]))
        paths = self._paths_for(
            tuple(file_id for postings in decoded.values() for file_id, _spans in postings)
        )
        for anchor in anchors:
            found[anchor] = tuple(
                sorted(
                    (
                        IndexedFile(path=paths[file_id], extents=spans)
                        for file_id, spans in decoded[anchor]
                    ),
                    key=lambda one: one.path,
                )
            )
        return found

    def validate_application_integrity(self) -> None:
        """Decode every packed application value before explicit prebuild reports ready.

        SQLite's structural check cannot see a truncated posting, bad compressed quote, or
        dangling packed identifier. Explicit prebuild is the readiness boundary, so it pays
        this full streaming pass and either causes acquisition to rebuild or fails; ordinary
        document queries retain their keyed warm path.
        """
        file_ids = {
            int(file_id) for (file_id,) in self.connection.execute("SELECT file_id FROM files")
        }
        quote_stream_ids, expected_short, quote_text_bytes = _validate_quote_streams(
            self.connection, file_ids
        )
        if len(quote_stream_ids) != self.quote_stream_count:
            raise SourceIndexDatabaseError("citation source-index quote stream count is corrupt")
        if quote_text_bytes != self.quote_text_bytes:
            raise SourceIndexDatabaseError("citation source-index quote text count is corrupt")
        _validate_direct_postings(self.connection, file_ids)
        _validate_short_postings(self.connection, quote_stream_ids, expected_short)
        _validate_call_literals(self.connection)
        _validate_stored_digest(self.connection)

    def _quotes(
        self, anchors: tuple[model.Anchor, ...]
    ) -> dict[model.Anchor, tuple[IndexedFile, ...]]:
        if not anchors:
            return {}
        self.quote_anchor_queries += len(anchors)
        targets = {anchor: model.normalised(anchor.text) for anchor in anchors}
        matched: dict[model.Anchor, dict[str, list[extents.QuoteMatch]]] = {
            anchor: {} for anchor in anchors
        }
        for anchor, target in targets.items():
            if not target:
                continue
            for raw_path, raw_text, raw_marks in self._candidate_streams(target):
                text = _unpack_text(bytes(raw_text))
                self.quote_candidate_streams_read += 1
                self.quote_candidate_text_bytes_read += len(text.encode("utf-8"))
                if target not in text:
                    continue
                stream = extents.CollapsedText(
                    text=text, marks=_unpack_marks(text, bytes(raw_marks))
                )
                path = str(raw_path)
                matched[anchor].setdefault(path, []).extend(
                    extents.quote_matches_in(anchor.text, stream)
                )
        return {anchor: self._quote_files(anchor, per_file) for anchor, per_file in matched.items()}

    def _candidate_streams(self, target: str) -> list[tuple[str, bytes, bytes]]:
        stream_ids = (
            self._short_stream_ids(target) if len(target) < 3 else self._fts_stream_ids(target)
        )
        found: list[tuple[str, bytes, bytes]] = []
        for offset in range(0, len(stream_ids), _SQLITE_PARAMETER_BATCH):
            batch = stream_ids[offset : offset + _SQLITE_PARAMETER_BATCH]
            parameters = ", ".join("?" for _stream_id in batch)
            found.extend(
                cast(
                    list[tuple[str, bytes, bytes]],
                    self.connection.execute(
                        "SELECT files.path, quote_streams.text, quote_streams.marks "
                        "FROM quote_streams JOIN files USING(file_id) "
                        f"WHERE quote_streams.stream_id IN ({parameters})",
                        batch,
                    ).fetchall(),
                )
            )
        return found

    def _short_stream_ids(self, target: str) -> tuple[int, ...]:
        self.quote_short_gram_lookups += 1
        row = self.connection.execute(
            "SELECT CAST(stream_ids AS BLOB) FROM quote_short_postings WHERE gram = ?",
            (target.encode("utf-8"),),
        ).fetchone()
        return () if row is None else _unpack_stream_ids(bytes(row[0]))

    def _fts_stream_ids(self, target: str) -> tuple[int, ...]:
        self.quote_index_lookups += 1
        trigrams = tuple(
            dict.fromkeys(target[index : index + 3] for index in range(len(target) - 2))
        )
        counts: dict[str, int] = {}
        for offset in range(0, len(trigrams), _SQLITE_PARAMETER_BATCH):
            batch = trigrams[offset : offset + _SQLITE_PARAMETER_BATCH]
            parameters = ", ".join("?" for _trigram in batch)
            counts.update(
                (str(term), int(documents))
                for term, documents in self.connection.execute(
                    f"SELECT term, doc FROM quote_vocab WHERE term IN ({parameters})",
                    batch,
                )
            )
        if len(counts) != len(trigrams):
            return ()
        rarest = min(trigrams, key=lambda one: (counts[one], one))
        query = '"' + rarest.replace('"', '""') + '"'
        return tuple(
            int(row_id)
            for (row_id,) in self.connection.execute(
                "SELECT rowid FROM quote_search WHERE quote_search MATCH ?", (query,)
            )
        )

    def _quote_files(
        self, anchor: model.Anchor, matches: dict[str, list[extents.QuoteMatch]]
    ) -> tuple[IndexedFile, ...]:
        found: list[IndexedFile] = []
        for path in sorted(matches):
            unique = tuple(
                dict.fromkeys(
                    sorted(
                        matches[path],
                        key=lambda one: (
                            one.source_byte_start,
                            one.source_byte_end,
                            one.start,
                            one.end,
                        ),
                    )
                )
            )
            widened = extents.widened_quotes(anchor.text, unique, self._calls(path))
            if widened:
                found.append(IndexedFile(path=path, extents=widened))
        return tuple(found)

    def _calls(self, path: str) -> tuple[grammars.CallLiteral, ...]:
        rows = self.connection.execute(
            "SELECT call_literals.text, start, end, argument_start_byte, argument_end_byte "
            "FROM call_literals JOIN files USING(file_id) "
            "WHERE files.path = ? ORDER BY call_literals.ordinal",
            (path,),
        )
        return tuple(
            grammars.CallLiteral(
                text=str(text),
                start=int(start),
                end=int(end),
                argument_start_byte=int(argument_start),
                argument_end_byte=int(argument_end),
            )
            for text, start, end, argument_start, argument_end in rows
        )


def _validate_quote_streams(
    connection: sqlite3.Connection, file_ids: set[int]
) -> tuple[set[int], dict[bytes, bytearray], int]:
    stream_ids: set[int] = set()
    expected_short: dict[bytes, bytearray] = {}
    text_bytes = 0
    for stream_id, file_id, raw_text, raw_marks in connection.execute(
        "SELECT stream_id, file_id, text, marks FROM quote_streams ORDER BY stream_id"
    ):
        numeric_stream_id = int(stream_id)
        if int(file_id) not in file_ids:
            raise SourceIndexDatabaseError(
                "citation source-index quote stream references a missing file"
            )
        text = _unpack_text(bytes(raw_text))
        _unpack_marks(text, bytes(raw_marks))
        stream_ids.add(numeric_stream_id)
        text_bytes += len(text.encode("utf-8"))
        _record_expected_short(expected_short, numeric_stream_id, text)
    return stream_ids, expected_short, text_bytes


def _record_expected_short(expected: dict[bytes, bytearray], stream_id: int, text: str) -> None:
    encoded_stream_id = _STREAM_ID.pack(stream_id)
    grams = set(text) | {text[index : index + 2] for index in range(len(text) - 1)}
    for gram in grams:
        expected.setdefault(gram.encode("utf-8"), bytearray()).extend(encoded_stream_id)


def _validate_direct_postings(connection: sqlite3.Connection, file_ids: set[int]) -> None:
    named_keys: set[bytes] = set()
    for anchor_key, anchor_kind, anchor_text in connection.execute(
        "SELECT anchor_key, anchor_kind, anchor_text FROM anchor_names"
    ):
        key = bytes(anchor_key)
        if key != _anchor_key(str(anchor_kind), str(anchor_text)):
            raise SourceIndexDatabaseError("citation source-index anchor key is corrupt")
        named_keys.add(key)
    posting_keys: set[bytes] = set()
    for anchor_key, raw_postings in connection.execute(
        "SELECT anchor_key, CAST(postings AS BLOB) FROM direct_postings"
    ):
        posting_keys.add(bytes(anchor_key))
        _validate_one_direct_posting(bytes(raw_postings), file_ids)
    if posting_keys != named_keys:
        raise SourceIndexDatabaseError(
            "citation source-index anchor names and postings do not match"
        )


def _validate_one_direct_posting(payload: bytes, file_ids: set[int]) -> None:
    for file_id, spans in _unpack_postings(payload):
        if file_id not in file_ids:
            raise SourceIndexDatabaseError(
                "citation source-index posting references a missing file"
            )
        if any(one.start < 1 or one.end < one.start for one in spans):
            raise SourceIndexDatabaseError("citation source-index extent coordinates are corrupt")


def _validate_short_postings(
    connection: sqlite3.Connection,
    stream_ids: set[int],
    expected: dict[bytes, bytearray],
) -> None:
    for gram, raw_stream_ids in connection.execute(
        "SELECT gram, CAST(stream_ids AS BLOB) FROM quote_short_postings"
    ):
        referenced = _unpack_stream_ids(bytes(raw_stream_ids))
        if any(stream_id not in stream_ids for stream_id in referenced):
            raise SourceIndexDatabaseError(
                "citation source-index short quote posting references a missing stream"
            )
        wanted = expected.pop(bytes(gram), None)
        if wanted is None or bytes(raw_stream_ids) != bytes(wanted):
            raise SourceIndexDatabaseError("citation source-index short quote postings are corrupt")
    if expected:
        raise SourceIndexDatabaseError("citation source-index short quote postings are corrupt")


def _validate_call_literals(connection: sqlite3.Connection) -> None:
    missing = connection.execute(
        "SELECT 1 FROM call_literals LEFT JOIN files USING(file_id) "
        "WHERE files.file_id IS NULL LIMIT 1"
    ).fetchone()
    if missing is not None:
        raise SourceIndexDatabaseError(
            "citation source-index call literal references a missing file"
        )


def _validate_stored_digest(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'application_sha256'"
    ).fetchone()
    if row is None or _application_digest(connection) != str(row[0]):
        raise SourceIndexDatabaseError(
            "citation source-index application payload checksum is corrupt"
        )


def _anchor_key(kind: str, text: str) -> bytes:
    digest = hashlib.sha256()
    digest.update(kind.encode())
    digest.update(b"\0")
    digest.update(text.encode())
    return digest.digest()


def _application_digest(connection: sqlite3.Connection) -> str:
    """Canonical checksum of every query-bearing row, including FTS term/doc pairs."""
    digest = hashlib.sha256(b"citation-source-index-application-v1\0")
    queries = (
        ("files", "SELECT file_id, path FROM files ORDER BY file_id"),
        (
            "anchor_names",
            "SELECT anchor_key, anchor_kind, anchor_text FROM anchor_names ORDER BY anchor_key",
        ),
        (
            "direct_postings",
            "SELECT anchor_key, CAST(postings AS BLOB) FROM direct_postings ORDER BY anchor_key",
        ),
        (
            "quote_streams",
            "SELECT stream_id, file_id, ordinal, text, marks FROM quote_streams ORDER BY stream_id",
        ),
        (
            "quote_instances",
            "SELECT term, doc FROM quote_instances ORDER BY doc, term",
        ),
        (
            "quote_short_postings",
            "SELECT gram, CAST(stream_ids AS BLOB) FROM quote_short_postings ORDER BY gram",
        ),
        (
            "call_literals",
            "SELECT file_id, ordinal, text, start, end, argument_start_byte, "
            "argument_end_byte FROM call_literals ORDER BY file_id, ordinal",
        ),
    )
    for table, query in queries:
        digest.update(table.encode())
        digest.update(b"\0")
        for row in connection.execute(query):
            for value in row:
                _digest_value(digest, value)
            digest.update(b"\xff")
    return digest.hexdigest()


def _digest_value(digest: _Digest, value: object) -> None:
    if isinstance(value, int):
        payload = str(value).encode()
        kind = b"i"
    elif isinstance(value, str):
        payload = value.encode()
        kind = b"s"
    elif isinstance(value, bytes):
        payload = value
        kind = b"b"
    else:
        raise SourceIndexDatabaseError(
            f"citation source-index contains unsupported {type(value).__name__} payload"
        )
    digest.update(kind)
    digest.update(struct.pack("<Q", len(payload)))
    digest.update(payload)


def _pack_extents(spans: tuple[extents.Extent, ...]) -> bytes:
    return b"".join(
        _EXTENT_RECORD.pack(one.start, one.end, _EXTENT_CODE[one.kind]) for one in spans
    )


def _pack_posting(file_id: int, spans: tuple[extents.Extent, ...]) -> bytes:
    if len(spans) > 0xFFFF:
        raise SourceIndexDatabaseError("citation anchor has too many extents in one file")
    return _POSTING_HEADER.pack(file_id, len(spans)) + _pack_extents(spans)


def _unpack_postings(payload: bytes) -> tuple[tuple[int, tuple[extents.Extent, ...]], ...]:
    found: list[tuple[int, tuple[extents.Extent, ...]]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < _POSTING_HEADER.size:
            raise SourceIndexDatabaseError("citation source-index posting header is corrupt")
        file_id, count = _POSTING_HEADER.unpack_from(payload, offset)
        offset += _POSTING_HEADER.size
        end = offset + count * _EXTENT_RECORD.size
        if end > len(payload):
            raise SourceIndexDatabaseError("citation source-index posting body is corrupt")
        found.append((file_id, _unpack_extents(payload[offset:end])))
        offset = end
    return tuple(found)


def _unpack_stream_ids(payload: bytes) -> tuple[int, ...]:
    if len(payload) % _STREAM_ID.size:
        raise SourceIndexDatabaseError("citation source-index short quote posting is corrupt")
    return tuple(
        _STREAM_ID.unpack_from(payload, offset)[0]
        for offset in range(0, len(payload), _STREAM_ID.size)
    )


def _unpack_extents(payload: bytes) -> tuple[extents.Extent, ...]:
    if len(payload) % _EXTENT_RECORD.size:
        raise SourceIndexDatabaseError("citation source-index extent posting is corrupt")
    found: list[extents.Extent] = []
    for offset in range(0, len(payload), _EXTENT_RECORD.size):
        start, end, kind_code = _EXTENT_RECORD.unpack_from(payload, offset)
        try:
            kind = _EXTENT_KIND[kind_code]
        except KeyError as error:
            raise SourceIndexDatabaseError(
                "citation source-index extent kind is corrupt"
            ) from error
        found.append(extents.Extent(start=start, end=end, kind=kind))
    return tuple(found)


def _occurrence_extents(lines: list[int]) -> tuple[extents.Extent, ...]:
    found: list[extents.Extent] = []
    for line in lines:
        if found and found[-1].end == line - 1:
            found[-1] = extents.Extent(found[-1].start, line, extents.OCCURRENCE)
        else:
            found.append(extents.Extent(line, line, extents.OCCURRENCE))
    return tuple(found)


def _pack_text(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"))


def _unpack_text(payload: bytes) -> str:
    try:
        return zlib.decompress(payload).decode("utf-8")
    except (UnicodeDecodeError, zlib.error) as error:
        raise SourceIndexDatabaseError("citation source-index quote text is corrupt") from error


def _pack_marks(marks: tuple[extents.WordMark, ...]) -> bytes:
    raw = b"".join(
        _MARK.pack(mark.collapsed_start, mark.collapsed_end, mark.line, mark.source_byte_start)
        for mark in marks
    )
    return zlib.compress(raw)


def _unpack_marks(text: str, payload: bytes) -> tuple[extents.WordMark, ...]:
    try:
        raw = zlib.decompress(payload)
    except zlib.error as error:
        raise SourceIndexDatabaseError("citation source-index word map is corrupt") from error
    if len(raw) % _MARK.size:
        raise SourceIndexDatabaseError("citation source-index word map is corrupt")
    found: list[extents.WordMark] = []
    for offset in range(0, len(raw), _MARK.size):
        start, end, line, source_start = _MARK.unpack_from(raw, offset)
        if start > end or end > len(text) or line < 1:
            raise SourceIndexDatabaseError("citation source-index word map is corrupt")
        found.append(
            extents.WordMark(
                collapsed_start=start,
                collapsed_end=end,
                line=line,
                source_byte_start=source_start,
                text=text[start:end],
            )
        )
    return tuple(found)
