"""Parse the prose citation form ``cit:([anchors], path:start-end)``.

The prefix and explicit path are mandatory. Anchors/sources use the table grammar and pool
identically. Unfenced, non-table prose is grouped at blank lines so a wrapped citation can
be recognized and reported at its starting line.

Superseded forms pair a backticked anchor with a parenthesized ``L`` range. A two-endpoint
range without an anchor is still reported as unanchored. A single number with no adjacent
anchor is not claimed because forms such as ``(L4)`` are also leaf shorthand; these are
counted as ``uncheckedProseRanges``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agents_remember.kernel.onboarding_doc import active_claim_lines
from agents_remember.memory_quality.style.citations import cells, model
from agents_remember.memory_quality.style.document_shape import inline_scan

CIT_MARK = "cit:("
ANCHOR_LIST_OPEN = "["
ANCHOR_LIST_CLOSE = "]"
PAREN_CLOSE = ")"

RANGE_BODY = "L\\d+(?:\\s*[-\u2013\u2014]\\s*L?\\d+)?"
# `X` (L47) -- the anchor sits outside the parentheses.
AFTER_ANCHOR = re.compile(rf"^\s*\(\s*(?P<range>{RANGE_BODY})\s*\)")
# (`X` L775-L795) and (`X`, L110-L112) -- the anchor sits inside them.
INSIDE_PARENS = re.compile(rf"^\s*,?\s*(?P<range>{RANGE_BODY})\s*\)")
# (L126-L173) -- nothing beside it at all.
BARE_PARENS = re.compile(rf"\(\s*(?P<range>{RANGE_BODY})\s*\)")
TWO_ENDPOINTS = re.compile("L\\d+\\s*[-\u2013\u2014]")

GRAMMAR = (
    "The form is `cit:([<anchor>, ...], <path>:<start>-<end>)` -- the `cit:` prefix is "
    "mandatory, the anchor list is bracketed, and the path is always explicit. An anchor is "
    "a backticked code identifier, a backticked `#`-prefixed markdown heading, or a "
    "double-quoted literal string. There is no `L` prefix: the range spelling is the same "
    "one the Source column uses."
)


@dataclass(frozen=True)
class Block:
    """One paragraph of prose, joined into a single string with its line numbers kept."""

    text: str
    starts: tuple[tuple[int, int], ...]

    def line_at(self, offset: int) -> int:
        found = self.starts[0][1]
        for start, line in self.starts:
            if start > offset:
                break
            found = line
        return found


@dataclass(frozen=True)
class Superseded:
    """One citation in the old spelling: where it is and what it says."""

    line: int
    text: str
    anchored: bool


@dataclass(frozen=True)
class ProseScan:
    """Everything one document's prose holds."""

    claims: tuple[model.Claim, ...]
    unparsed: tuple[Superseded, ...]
    superseded: tuple[Superseded, ...]
    unchecked_ranges: int


@dataclass
class ProseParts:
    """The mutable accumulator one document's blocks are read into."""

    claims: list[model.Claim]
    unparsed: list[Superseded]
    superseded: list[Superseded]
    covered: list[tuple[int, int]]
    unchecked: int = 0


def blocks(lines: list[str], occupied: set[int] | None = None) -> list[Block]:
    """Prose paragraphs: unfenced, outside any table, split at blank lines."""
    lines = active_claim_lines(lines)
    occupied = cells.table_lines(lines) if occupied is None else occupied
    found: list[Block] = []
    pieces: list[str] = []
    starts: list[tuple[int, int]] = []
    width = 0
    for index, line in inline_scan.unfenced_lines(lines):
        if index in occupied or not line.strip():
            if pieces:
                found.append(Block(text=" ".join(pieces), starts=tuple(starts)))
            pieces, starts, width = [], [], 0
            continue
        starts.append((width, index + 1))
        pieces.append(line.strip())
        width += len(line.strip()) + 1
    if pieces:
        found.append(Block(text=" ".join(pieces), starts=tuple(starts)))
    return found


def misplaced_in_tables(lines: list[str], occupied: set[int] | None = None) -> list[Superseded]:
    """A ``cit:`` written into a table cell -- the wrong serialisation, one report per row.

    Table lines are kept out of the prose scan so no row is read twice, and that is exactly
    what would make this silent: a construct that looks checked and is not. A BACKTICKED
    ``cit:`` is still a quotation of the grammar and is still not a citation, here as
    anywhere else.
    """
    occupied = cells.table_lines(lines) if occupied is None else occupied
    found: list[Superseded] = []
    for index, line in inline_scan.unfenced_lines(lines):
        if index not in occupied:
            continue
        spans = inline_scan.code_span_ranges(line)
        at = line.find(CIT_MARK)
        while at >= 0:
            if inline_scan.enclosing_span_end(at, spans) is None:
                found.append(Superseded(line=index + 1, text=line.strip()[:80], anchored=True))
                break
            at = line.find(CIT_MARK, at + len(CIT_MARK))
    return found


@dataclass(frozen=True)
class Parts:
    """Where a ``cit:`` body's two halves are, as offsets into that body.

    The fixer rewrites the source half in place and must not disturb a character of the
    rest, so the offsets are read here rather than by a second parse that could disagree
    with this one about where the anchor list ends.
    """

    anchors: tuple[int, int]
    sources: int


def parts(inner: str) -> Parts | None:
    """``[anchors], sources`` split into offsets, or ``None`` when it is not that shape."""
    spans = inline_scan.code_span_ranges(inner)
    opened = inner.find(ANCHOR_LIST_OPEN)
    if opened < 0 or inline_scan.enclosing_span_end(opened, spans) is not None:
        return None
    closed = model.matching(inner, opened, spans, ANCHOR_LIST_CLOSE)
    if closed is None:
        return None
    rest = inner[closed + 1 :]
    stripped = rest.lstrip()
    if not stripped.startswith(","):
        return None
    return Parts(anchors=(opened + 1, closed), sources=closed + 1 + len(rest) - len(stripped) + 1)


def parse_citation(inner: str, line: int) -> model.Claim | None:
    """``[anchors], sources`` into a claim, or ``None`` when it is not that shape."""
    found = parts(inner)
    if found is None:
        return None
    anchors, skipped = model.anchors_in(inner[found.anchors[0] : found.anchors[1]])
    citations, malformed = model.citations_in(inner[found.sources :])
    return model.Claim(
        line=line,
        anchors=anchors,
        citations=citations,
        malformed=malformed,
        unchecked_spans=skipped,
    )


def scan_block(block: Block, found: ProseParts) -> None:
    """Every ``cit:`` in one block, and the extent each one covers."""
    spans = inline_scan.code_span_ranges(block.text)
    index = 0
    while True:
        at = block.text.find(CIT_MARK, index)
        if at < 0:
            return
        index = at + len(CIT_MARK)
        if inline_scan.enclosing_span_end(at, spans) is not None:
            continue
        opened = at + len(CIT_MARK) - 1
        closed = model.matching(block.text, opened, spans, PAREN_CLOSE)
        line = block.line_at(at)
        if closed is None:
            found.unparsed.append(
                Superseded(line=line, text=block.text[at : at + 60], anchored=True)
            )
            continue
        found.covered.append((at, closed + 1))
        index = closed + 1
        claim = parse_citation(block.text[opened + 1 : closed], line)
        if claim is None:
            found.unparsed.append(
                Superseded(line=line, text=block.text[at : closed + 1], anchored=True)
            )
            continue
        found.claims.append(claim)


def record(block: Block, at: int, end: int, anchored: bool, found: ProseParts) -> None:
    found.covered.append((at, end))
    found.superseded.append(
        Superseded(
            line=block.line_at(at),
            text=block.text[max(0, at - 40) : end].strip(),
            anchored=anchored,
        )
    )


def anchor_extents(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Every code span and every quoted literal -- the two things that can anchor a range."""
    quoted = [
        (match.start(), match.end()) for match in model.QUOTE_PATTERN.finditer(model.masked(text))
    ]
    return sorted([*spans, *quoted])


def scan_anchored(block: Block, found: ProseParts) -> None:
    """``X (L47)`` and ``(X L775-L795)`` -- an anchor with a range beside it."""
    spans = inline_scan.code_span_ranges(block.text)
    for start, end in anchor_extents(block.text, spans):
        if any(low <= start < high for low, high in found.covered):
            continue
        outside = AFTER_ANCHOR.match(block.text[end:])
        if outside is not None:
            record(block, start, end + outside.end(), True, found)
            continue
        inside = INSIDE_PARENS.match(block.text[end:])
        if inside is not None and block.text[:start].rstrip().endswith("("):
            record(block, start, end + inside.end(), True, found)


def scan_bare(block: Block, found: ProseParts) -> None:
    """``(L126-L173)`` with nothing beside it: no anchor, no file, no referent.

    A single number here is NOT claimed -- ``(L4)`` is this repository's leaf shorthand as
    often as a line -- but two endpoints cannot be a leaf identifier, so they are.
    """
    for match in BARE_PARENS.finditer(block.text):
        if any(low <= match.start() < high for low, high in found.covered):
            continue
        if not TWO_ENDPOINTS.search(match.group("range")):
            found.unchecked += 1
            continue
        record(block, match.start(), match.end(), False, found)


def scan(lines: list[str], occupied: set[int] | None = None) -> ProseScan:
    """Every citation one document's prose holds, current spelling and superseded."""
    parts = ProseParts(claims=[], unparsed=[], superseded=[], covered=[])
    for block in blocks(lines, occupied):
        parts.covered.clear()
        scan_block(block, parts)
        scan_anchored(block, parts)
        scan_bare(block, parts)
    return ProseScan(
        claims=tuple(parts.claims),
        unparsed=tuple(parts.unparsed),
        superseded=tuple(parts.superseded),
        unchecked_ranges=parts.unchecked,
    )
