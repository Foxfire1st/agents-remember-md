"""Publish one validated batch of citation edits without overwriting a changed document.

The application owns write-scope authorization; the source-index owner leases an immutable
generation. Neither supplies a memory-file mutex. Revalidation detects conflicts observed
before atomic replacement; it cannot exclude an uncooperative writer after that check.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

from agents_remember.kernel.atomic_write import atomic_write_bytes
from agents_remember.memory_quality.style.citations import deterministic_projection, source_index
from agents_remember.memory_quality.style.citations.editing import Site, rewritten


@dataclass(frozen=True)
class Edit:
    """An accepted source-cell rewrite, optionally carrying an exact-move projection."""

    site: Site
    was: str
    now: str
    repairing: bool
    projection: deterministic_projection.Projection | None = None


@dataclass
class DocumentTransaction:
    """Original bytes and accepted edits for one document in one source-index lease."""

    path: Path
    relative: str
    original: bytes
    snapshot_id: str
    edits: list[Edit] = field(default_factory=list)

    def render(self) -> bytes:
        """Compose source cells and their generated history in the same final bytes."""
        lines = self.original.decode("utf-8").split("\n")
        changes = [(edit.site, edit.now) for edit in self.edits]
        bullets = [
            edit.projection.history_bullet
            for edit in self.edits
            if edit.projection is not None and edit.projection.history_bullet is not None
        ]
        heading = deterministic_projection.history_section_line(lines)
        if bullets and heading is not None:
            changes.append(deterministic_projection.history_edit(lines, heading, bullets))
        return "\n".join(rewritten(lines, changes)).encode("utf-8")

    def unchanged(self, index: source_index.RepositoryIndex) -> bool:
        """Revalidate the exact read and every cell against the same frozen authority.

        Snapshot identity comes from the held index lease, including the operator's
        expected-snapshot assertion. This is not a new live source-tree freshness check.
        """
        try:
            current = self.path.read_bytes()
        except FileNotFoundError:
            return False
        if current != self.original or index.snapshot_id != self.snapshot_id:
            return False
        lines = current.decode("utf-8").split("\n")
        return all(self._cell_unchanged(lines, edit, index.snapshot_id) for edit in self.edits)

    @staticmethod
    def _cell_unchanged(lines: list[str], edit: Edit, snapshot_id: str) -> bool:
        if not deterministic_projection.verify_unchanged(lines, edit.site, edit.was):
            return False
        projection = edit.projection
        return projection is None or (
            projection.snapshot_id == snapshot_id
            and projection.was == edit.was
            and projection.now == edit.now
            and projection.prior_claim_digest == sha256(edit.was.encode("utf-8")).hexdigest()
        )

    def publish(self, index: source_index.RepositoryIndex) -> str | None:
        """Return the successful final-byte digest, or refuse without writing anything."""
        body = self.render()
        if not self.unchanged(index):
            return None
        atomic_write_bytes(self.path, body)
        return sha256(body).hexdigest()

    def preview(self, index: source_index.RepositoryIndex) -> str | None:
        """Return the prospective final-byte digest without publishing the batch."""
        body = self.render()
        return sha256(body).hexdigest() if self.unchanged(index) else None

    def projections(self, digest: str) -> list[deterministic_projection.Projection]:
        return [
            replace(edit.projection, new_document_digest=digest)
            for edit in self.edits
            if edit.projection is not None
        ]
