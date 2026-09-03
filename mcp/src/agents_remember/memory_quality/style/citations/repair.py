"""Plan exact-name tier-1 citation repairs.

Generate one extent per anchor and merge only overlapping/adjacent extents in the same
file; never widen discontiguous anchors into one enclosing range. Search cited files first,
then the wider code tree only when the anchor is absent there. Repoint only a unique exact-
name match.

Renames, deletions, typos, and ambiguity are declined because syntax cannot distinguish
them safely. Tree-wide mode plans only failing citations. Document-scoped normalization
also regenerates passing ranges through ``migration._scoped`` so verified mention spans
are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agents_remember.memory_quality.style.citations import extents, model, symbol_index
from agents_remember.memory_quality.style.citations.range_resolution import Sources
from agents_remember.memory_quality.style.citations.resolution import Trees

ANCHOR_ABSENT = "anchor_absent"
ANCHOR_AMBIGUOUS = "anchor_ambiguous"

DECLINE_REMEDIATION = {
    ANCHOR_ABSENT: (
        "--fix repairs pure moves only, and this is not one: the anchor keeps its name "
        "nowhere in the tree. Read the CLAIM rather than the pointer -- a symbol that "
        "exists nowhere usually means the behaviour changed, not that a number went stale."
    ),
    ANCHOR_AMBIGUOUS: (
        "--fix rewrites a path only when the anchor resolves UNIQUELY, and it never picks "
        "between candidates by similarity: an agent that READS two functions can tell a "
        "mapper from a validator, a string distance cannot. Read the candidates and cite "
        "the one the claim is about."
    ),
}


@dataclass(frozen=True)
class ResolvedLocation:
    """One anchor's chosen extent, exactly as the shared oracle resolved it.

    Carried on Repair so the deterministic projection binds the extent the
    repair actually followed -- the in-file tiebreaker when the cited range picks one
    candidate, the tree-wide Sightings.unique definition otherwise -- without a
    second lookup or a second authority for the answer.
    """

    anchor: model.Anchor
    path: str
    extent: extents.Extent


@dataclass(frozen=True)
class Repair:
    """The source list ``--fix`` would write for one claim."""

    sources: tuple[str, ...]
    locations: tuple[ResolvedLocation, ...] = ()


@dataclass(frozen=True)
class Decline:
    """Why one claim stays for the curator, and the facts it needs to work it down."""

    code: str
    anchor: model.Anchor | None
    detail: str

    @property
    def message(self) -> str:
        return f"{self.detail} {DECLINE_REMEDIATION[self.code]}"


@dataclass(frozen=True)
class Cited:
    """One of a claim's sources and the file it named, when that file still exists."""

    citation: model.Citation
    file: Path | None


def targets(claim: model.Claim, trees: Trees) -> tuple[Cited, ...]:
    return tuple(
        Cited(citation=citation, file=trees.resolve(citation.path)) for citation in claim.citations
    )


def chosen(found: tuple[extents.Extent, ...], citation: model.Citation) -> extents.Extent | None:
    """The one extent this citation means, or ``None`` when the file offers a choice.

    The claim's own range is the tiebreaker inside a file, for the same reason the cited
    PATH is the tiebreaker across files: it is the author's statement about where they
    were pointing, and it is the only signal available that is not a similarity score.
    """
    if len(found) == 1:
        return found[0]
    overlapping = [one for one in found if one.holds(citation.start, citation.end)]
    return overlapping[0] if len(overlapping) == 1 else None


@dataclass
class _Plan:
    """One claim's repair as it accumulates: the spans found, and the first refusal."""

    spans: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    reached: set[str] = field(default_factory=set)
    relocated: bool = False
    declined: Decline | None = None
    locations: list[ResolvedLocation] = field(default_factory=list)

    def add(self, anchor: model.Anchor, path: str, extent: extents.Extent) -> None:
        self.spans.setdefault(path, []).append((extent.start, extent.end))
        self.reached.add(path)
        self.locations.append(ResolvedLocation(anchor=anchor, path=path, extent=extent))

    def refuse(self, decline: Decline) -> None:
        self.declined = self.declined or decline


def plan(
    claim: model.Claim,
    trees: Trees,
    sources: Sources,
    sightings: dict[model.Anchor, symbol_index.Sightings],
) -> Repair | Decline:
    """The tiebreaker, applied to one claim."""
    cited = targets(claim, trees)
    found = _Plan()
    for anchor in claim.anchors:
        _place(anchor, cited, found, sources, sightings)
    if found.declined is not None:
        return found.declined
    return Repair(
        sources=tuple(_written(found, cited, trees)),
        locations=tuple(found.locations),
    )


def _carried(cited: tuple[Cited, ...], found: _Plan, trees: Trees) -> list[str]:
    """The sources that survive unchanged because nothing here could regenerate them.

    A source into somebody else's tree is ALWAYS kept: it resolves in neither root by
    construction, it is what the quoted-anchor form exists for, and deleting it would
    delete the only evidence a third-party claim has.

    A live source that holds none of the claim's anchors is kept only while nothing MOVED.
    Once an anchor has been resolved from the wider tree, an unanchored cited file is the
    location it was resolved out of, and keeping it would leave the stale pointer sitting
    beside its own replacement -- which is the exact damage this whole check exists to end.
    A source whose file is gone is never kept: it points at nothing.
    """
    return [
        one.citation.text
        for one in cited
        if one.citation.path not in found.reached
        and (
            (one.file is None and not trees.ours(one.citation.path))
            or (one.file is not None and not found.relocated)
        )
    ]


def _place(
    anchor: model.Anchor,
    cited: tuple[Cited, ...],
    found: _Plan,
    sources: Sources,
    sightings: dict[model.Anchor, symbol_index.Sightings],
) -> None:
    """Where this anchor's range comes from: a cited file first, the wider tree second."""
    placed = False
    for one in cited:
        if one.file is None:
            continue
        holds = sources.view(one.file, one.citation.path).extents(anchor)
        if not holds:
            continue
        extent = chosen(holds, one.citation)
        if extent is None:
            found.refuse(_ambiguous_in_file(anchor, one, holds))
            return
        found.add(anchor, one.citation.path, extent)
        placed = True
    if placed:
        return
    _place_elsewhere(anchor, found, sightings[anchor])


def _place_elsewhere(anchor: model.Anchor, found: _Plan, seen: symbol_index.Sightings) -> None:
    unique = seen.unique
    if unique is None:
        found.refuse(
            Decline(
                code=ANCHOR_ABSENT if not seen.files else ANCHOR_AMBIGUOUS,
                anchor=anchor,
                detail=f"{anchor.written} is in none of the cited files and "
                f"{symbol_index.described(anchor, seen)}.",
            )
        )
        return
    found.add(anchor, unique.path, unique.extent)
    found.relocated = True


def _ambiguous_in_file(
    anchor: model.Anchor, one: Cited, holds: tuple[extents.Extent, ...]
) -> Decline:
    written = ", ".join(f"{one.citation.path}:{e.start}-{e.end}" for e in holds)
    return Decline(
        code=ANCHOR_AMBIGUOUS,
        anchor=anchor,
        detail=f"{anchor.written} occurs {len(holds)} times in {one.citation.path} and the "
        f"cited range {one.citation.text} picks out none of them: {written}.",
    )


def _written(found: _Plan, cited: tuple[Cited, ...], trees: Trees) -> list[str]:
    """The generated source list: one range per anchor, merged per file, then what survives.

    Merged only where two extents overlap or abut, so two anchors in different parts of one
    file stay two ranges. Ordered by path and line, which is what makes a second run of
    ``--fix`` a byte-for-byte no-op.
    """
    generated = [
        f"{path}:{start}-{end}"
        for path in sorted(found.spans)
        for start, end in extents.merged(found.spans[path])
    ]
    return generated + _carried(cited, found, trees)
