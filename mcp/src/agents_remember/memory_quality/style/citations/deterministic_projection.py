"""Deterministic anchor-to-range projection (CCR-R10).

When a claim's exact anchor resolves uniquely in the frozen source-index snapshot, this
transaction projects the claim's current path/range mechanically - the same exact-name
oracle :mod: uses (symbol_index.locate and Sightings.unique, language extents,
and the source-index snapshot) - and binds the source-index snapshot, the prior
claim/citation digest, the anchor, the resolved definition extent, the new document
digest, and the repair-tool version. The history/body rails stay satisfied through an
explicit generated no-content-impact Update History bullet staged inside the same edit
transaction, so a mechanically moved range is never an untraced body edit and a
history-only refresh is never unmarked.

Every other case refuses deterministically and routes to the existing actionable
finding path: multiple definitions, multiple unparsed occurrences, parsed mention-only
anchors, renamed symbols, deleted symbols, malformed claims, stale snapshots, and
conflicting writes. No similarity, filename, or prose search authority is introduced,
no old range is accepted as a fallback, and canonical Markdown remains the sole memory
content - the binding travels in the repair payload, never a sidecar.

This module owns projection bindings and source-cell preconditions. The document transaction
composes accepted projections and checks their preconditions at publication; resolution
semantics stay with the shared oracle, without a second citation authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from agents_remember.memory_quality.style.citations import model, repair, source_index
from agents_remember.memory_quality.style.citations.editing import Site

OPERATION = "citation_deterministic_projection"
REPAIR_TOOL_VERSION = "ccr-r10@v1"
NO_IMPACT_MARKER = "No content impact:"
HISTORY_HEADING = "## Update History"

PROJECTION_CONFLICT = "projection_conflicting_write"
PROJECTION_EMPTY = "projection_no_resolved_extent"


def now_utc() -> datetime:
    """The projection timestamp source, injectable so a replay is byte-for-byte."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class ResolvedExtentInfo:
    """One anchor's resolved extent as the oracle chose it, ready to bind."""

    anchor: str
    path: str
    start: int
    end: int
    kind: str

    @property
    def written(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


@dataclass(frozen=True)
class Projection:
    """One deterministic anchor-to-range rewrite, with its full binding."""

    document: str
    line: int
    anchors: tuple[str, ...]
    was: str
    now: str
    extents: tuple[ResolvedExtentInfo, ...]
    snapshot_id: str
    prior_claim_digest: str
    new_document_digest: str | None
    history_bullet: str | None
    at: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "line": self.line,
            "anchors": list(self.anchors),
            "was": self.was,
            "now": self.now,
            "resolvedExtents": [
                {
                    "anchor": one.anchor,
                    "path": one.path,
                    "start": one.start,
                    "end": one.end,
                    "kind": one.kind,
                }
                for one in self.extents
            ],
            "snapshotId": self.snapshot_id,
            "priorClaimDigest": self.prior_claim_digest,
            "newDocumentDigest": self.new_document_digest,
            "historyBullet": self.history_bullet,
            "at": self.at,
            "repairToolVersion": self.version,
        }


@dataclass(frozen=True)
class ProjectionDecline:
    """Why one claim stayed on the curator path instead of being projected."""

    code: str
    anchor: str | None
    message: str


def history_section_line(lines: list[str]) -> int | None:
    """The 1-based line of the Update History heading, or None when absent.

    A document without the canonical section gets no generated bullet: inventing a
    section from a range rewrite would be a structural decision beyond the mechanical
    projection, and there is no history rail to satisfy on a document that has none.
    """
    for index, line in enumerate(lines):
        if line.strip() == HISTORY_HEADING:
            return index + 1
    return None


def history_bullet(
    *,
    at: datetime,
    snapshot_id: str,
    extents: tuple[ResolvedExtentInfo, ...],
) -> str:
    """The generated no-content-impact bullet, newest-first by construction.

    The stamp is a full offset-bearing ISO 8601 datetime so the Update History checker
    parses and orders it, and the text carries the explicit no-impact marker so
    closeout's sidecar rails classify the mechanical refresh as attested rather than as
    an unmarked history-only edit.
    """
    stamp = at.astimezone(UTC).isoformat(timespec="seconds")
    quoted = "; ".join(one.written for one in extents)
    return (
        f"- {stamp}: Generated citation repair: "
        f"{'; '.join(one.anchor for one in extents)} repointed to {quoted}. "
        f"{NO_IMPACT_MARKER} mechanical anchor-range projection bound to citation "
        f"source snapshot {snapshot_id}; claim bytes unchanged; generated by "
        f"{REPAIR_TOOL_VERSION}."
    )


@dataclass(frozen=True)
class ProjectionRequest:
    """The transaction envelope: one claim, its read, and the frozen generation."""

    lines: list[str]
    site: Site
    relative: str
    claim: model.Claim
    outcome: repair.Repair
    index: source_index.RepositoryIndex
    now: datetime
    history_line: int | None


def plan_projection(request: ProjectionRequest) -> Projection | ProjectionDecline:
    """One claim's deterministic projection, or the concrete refusal.

    lines is the transaction's read of the document; was is read from the live
    source cell, so the recorded prior digest always binds the exact bytes the rewrite
    replaces. new_document_digest is left for the caller, which alone knows the
    document's complete edit batch including any grouped history bullets.
    """
    was = request.lines[request.site.line - 1][request.site.start : request.site.end].strip()
    now_text = "; ".join(dict.fromkeys(request.outcome.sources))
    resolved: list[ResolvedExtentInfo] = []
    for anchor in request.claim.anchors:
        matched = [one for one in request.outcome.locations if one.anchor == anchor]
        if len(matched) != 1:
            return ProjectionDecline(
                code=PROJECTION_EMPTY,
                anchor=anchor.written,
                message=(
                    f"{anchor.written} resolved to {len(matched)} extent(s) inside the "
                    "repair's own oracle, so no single deterministic projection exists"
                ),
            )
        one = matched[0]
        resolved.append(
            ResolvedExtentInfo(
                anchor=anchor.written,
                path=one.path,
                start=one.extent.start,
                end=one.extent.end,
                kind=one.extent.kind,
            )
        )
    return Projection(
        document=request.relative,
        line=request.claim.line,
        anchors=tuple(one.anchor for one in resolved),
        was=was,
        now=now_text,
        extents=tuple(resolved),
        snapshot_id=request.index.snapshot_id,
        prior_claim_digest=sha256(was.encode("utf-8")).hexdigest(),
        new_document_digest=None,
        history_bullet=(
            history_bullet(
                at=request.now, snapshot_id=request.index.snapshot_id, extents=tuple(resolved)
            )
            if request.history_line is not None
            else None
        ),
        at=request.now.astimezone(UTC).isoformat(timespec="seconds"),
        version=REPAIR_TOOL_VERSION,
    )


def verify_unchanged(lines: list[str], site: Site, expected: str) -> bool:
    """The transaction's precondition: the cell still holds what the plan bound.

    The document transaction checks this immediately before publication, together with
    the complete original document and the still-leased source snapshot.
    """
    if not 1 <= site.line <= len(lines):
        return False
    line = lines[site.line - 1]
    return (
        0 <= site.start <= site.end <= len(line) and line[site.start : site.end].strip() == expected
    )


def conflicting_write_decline(relative: str, line: int, anchor: str | None) -> ProjectionDecline:
    return ProjectionDecline(
        code=PROJECTION_CONFLICT,
        anchor=anchor,
        message=(
            f"{relative}:{line} no longer matches its planned document, source cell, or "
            f"leased source snapshot at publication; refusing the entire document batch"
        ),
    )


def history_edit(lines: list[str], heading_line: int, bullets: list[str]) -> tuple[Site, str]:
    """One edit that inserts every generated bullet directly under the heading.

    The heading element is replaced by itself plus the bullets, so the existing blank
    line and older entries keep their own elements and the inserted bullets sit at the
    top of the section - newest-first by construction.
    """
    element = lines[heading_line - 1]
    separator = "\r\n" if element.endswith("\r") else "\n"
    text = element.removesuffix("\r") + separator + separator.join(bullets)
    return Site(line=heading_line, start=0, end=len(element)), text
