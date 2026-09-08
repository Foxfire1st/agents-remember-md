"""Migrate superseded citation tables and prose to the anchored format.

A table rewrite updates its header, delimiter, and every row in one write. Rows that
cannot be converted retain their original evidence verbatim in the Source cell so the
new checker reports them individually. The migration never invents an anchor, carries an
old line number forward, uses fuzzy matching, or deletes a claim.

A verified old range may disambiguate identical anchors but never supplies output lines;
generated extents come from the same resolver as ``--fix``. Rows with several plausible
anchors decline to tier 2. The caller must establish that the target is a leaf memory
worktree before this module writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents_remember.kernel.onboarding_doc import active_claim_lines
from agents_remember.memory_quality.integrity.onboarding_drift_check.discovery import (
    parse_table_metadata,
    rel,
)
from agents_remember.memory_quality.style.citations import (
    cells,
    drafts,
    extents,
    model,
    old_form,
    prose,
    range_resolution,
    repair,
    source_index,
    symbol_index,
    work_order,
)
from agents_remember.memory_quality.style.citations.drafts import Draft, Result, Subject, TableDraft
from agents_remember.memory_quality.style.citations.editing import Documents, Site, rewritten
from agents_remember.memory_quality.style.citations.range_resolution import Sources
from agents_remember.memory_quality.style.citations.resolution import Trees, operation_trees
from agents_remember.memory_quality.style.document_shape import inline_scan, tables

OPERATION = "citation_migrate"
# A verified quotation spanning more than half a file is declined for curator review;
# migration must not turn a near-whole-file range into generated evidence automatically.
VERIFIED_SPAN_FILE_SHARE = 0.5
# Every superseded prose shape puts an `L` range inside parentheses, so a line holding no
# `(` and no `L<digit>` can hold no citation. The scan below is the expensive part of a pass
# and this rejects most of the tree's prose before any of it runs.
MAY_CITE = re.compile(r"\(|L\d")
HEADER_ROW = "| Finding | Anchor | Source |"
DELIMITER_ROW = "| --- | --- | --- |"
FALLBACK_MARKER = tables.NO_CITATION_MARKER
Sightings = dict[model.Anchor, symbol_index.Sightings]
# One body row after placement: its draft, its claim, the evidence it keeps if it could not
# be converted, and the generated source list if it could.
Placed = tuple["Draft | None", str, str, "str | None"]


@dataclass
class Pass:
    """The document pass and immutable source generation every reader below needs.

    ``sources`` caches selected memory documents and cited slices. Repository-wide anchor
    acquisition comes from ``index`` and performs no per-document source tokenization.
    """

    onboarding_root: Path
    trees: Trees
    index: source_index.RepositoryIndex
    sources: Sources
    result: Result


def subject_of(document: Path, onboarding_root: Path) -> Subject:
    """The card's declared path and repository -- what its own links are written against."""
    try:
        metadata = parse_table_metadata(document)
    except (OSError, UnicodeDecodeError):  # pragma: no cover - guarded by the walk
        metadata = {}
    return Subject(
        document=document,
        relative=rel(document, onboarding_root),
        path=metadata.get("path", "").strip(),
        repository=metadata.get("repository", "").strip(),
    )


def row_anchors(finding: str, citations: str) -> tuple[tuple[model.Anchor, ...], str]:
    """The anchors a row STATES, or the code to decline under when it states none.

    The Citations cell wins because an anchor written beside its own range is a pairing the
    author made. The Finding sentence is read only when it offers exactly one candidate.
    """
    named, _ = model.anchors_in(citations)
    if named:
        return named, ""
    found, _ = model.anchors_in(finding)
    if len(found) == 1:
        return found, ""
    return (), drafts.ANCHOR_CHOICE_NEEDED if found else drafts.ANCHOR_ABSENT_FROM_ROW


def row_paths(cell: str, subject: Subject, run: Pass) -> tuple[tuple[str, ...], str]:
    """Every path this Source cell resolves to, or the code to decline under."""
    targets = old_form.link_targets(cell)
    if not targets:
        return (), drafts.SOURCE_NOT_A_PATH
    found = [
        old_form.resolved_path(
            one, subject.document, run.onboarding_root, subject.repository, run.trees
        )
        for one in targets
    ]
    missing = [one for one, got in zip(targets, found, strict=True) if got is None]
    if not missing:
        return tuple(dict.fromkeys(one for one in found if one is not None)), ""
    unexpressable = any(old_form.URL_MARK in one for one in missing)
    return (), drafts.SOURCE_NOT_A_PATH if unexpressable else drafts.SOURCE_UNRESOLVABLE


def verified(draft: Draft, run: Pass) -> old_form.Span | None:
    """The old range, but only where one file is cited and every anchor is proven inside it."""
    target = run.trees.resolve(draft.paths[0]) if len(draft.paths) == 1 else None
    if target is None:
        return None
    return old_form.verified_hint(draft.anchors, draft.raw_span, run.sources.lines(target))


def _not_in_range_detail(draft: Draft, target: Path, run: Pass) -> str:
    """Report the anchor's actual lines in the cited file without guessing another file."""
    lines = run.sources.lines(target)
    placed = {
        one.written: found
        for one in draft.anchors
        if (found := range_resolution.elsewhere_in_file(one, lines))
    }
    if not placed:
        return (
            f"{draft.text!r} does not hold its anchor in the range it gives, and no anchor "
            f"occurs anywhere in {draft.paths[0]}. That evidence leaves the cause unresolved: "
            f"the citation may have the wrong path, the construct may have moved or been "
            f"renamed, or it may have been deleted and the claim may now be stale."
        )
    where = "; ".join(
        f"{name} at {', '.join(str(at) for at in found)}" for name, found in placed.items()
    )
    return (
        f"{draft.text!r} does not hold its anchor in the range it gives, but the anchor IS in "
        f"{draft.paths[0]} -- {where}. The RANGE is stale, not the path."
    )


def plan_row(subject: Subject, line: int, texts: tuple[str, str, str], run: Pass) -> Draft:
    """One old row (finding, citations, source) read into a draft."""
    finding, citations, source = texts
    draft = Draft(
        subject=subject, line=line, kind=drafts.TABLE_ROW, site=Site(line, 0, 0), text=finding
    )
    draft.candidates = model.anchors_in(finding)[0]
    draft.raw_span = old_form.old_span(citations)
    paths, path_code = row_paths(source, subject, run)
    draft.paths = paths
    anchors, anchor_code = row_anchors(finding, citations)
    draft.anchors = anchors
    if path_code:
        draft.refuse(path_code, f"Source cell {source!r} names no usable path.")
    elif anchor_code:
        draft.refuse(anchor_code, _anchor_detail(anchor_code, draft))
    elif _is_note(citations):
        draft.refuse(
            drafts.CITATIONS_NOTE_DROPPED, f"Citations cell {citations!r} is a note, not a range."
        )
    else:
        draft.hint = verified(draft, run)
    return draft


def _is_note(citations: str) -> bool:
    """Whether the Citations cell holds prose that converting the row would DISCARD.

    Judged on the cell alone, because the cell is what would be lost. A marker is the empty
    state, a range becomes the generated one, and an anchor moves to the Anchor column --
    ``` `## Scoping` ``` is not a note but ``Source discovery checked`` is, and the second
    has nowhere to go in a three-column table.
    """
    return (
        not old_form.is_marker(citations)
        and old_form.old_span(citations) is None
        and not model.anchors_in(citations)[0]
    )


def _anchor_detail(code: str, draft: Draft) -> str:
    if code == drafts.ANCHOR_CHOICE_NEEDED:
        written = ", ".join(one.written for one in draft.candidates)
        return f"The Finding names {len(draft.candidates)} anchor candidates: {written}."
    return "The row names no anchor-shaped span in either its Finding or its Citations cell."


def plan_table(
    subject: Subject,
    header: tables.Row,
    body: list[tables.Row],
    columns: tuple[str, ...],
    run: Pass,
) -> TableDraft:
    """One superseded table, every row read. A placeholder row carries ``None`` for its draft."""
    at = {name: columns.index(name) for name in columns}
    source_at = at[old_form.SOURCE_PATH_COLUMN]
    drafted: list[tuple[Draft | None, str, str]] = []
    for row in body:
        texts = (
            _cell(row, at[old_form.FINDING_COLUMN]),
            _cell(row, at.get(old_form.CITATIONS_COLUMN)),
            _cell(row, source_at),
        )
        drafted.append(_plan_cells(subject, row, texts, run))
    return TableDraft(
        subject=subject,
        header=header.index + 1,
        rows=drafted,
        marker=old_form.marker_of([_cell(row, source_at) for row in body]) or FALLBACK_MARKER,
    )


def _plan_cells(
    subject: Subject, row: tables.Row, texts: tuple[str, str, str], run: Pass
) -> tuple[Draft | None, str, str]:
    """A row is the table's empty state, a claim citing no file, or something to convert.

    The third element is the row's own EVIDENCE, kept verbatim for a row that cannot be
    converted: the Source Path cell where there is one, and otherwise the bare range the
    Citations cell held. Both are sources in the superseded spelling, so the shipped checker
    reports the row rather than passing it, and the curator is handed what the author wrote.
    """
    finding, citations, source = texts
    if old_form.is_marker(source) and old_form.old_span(citations) is None:
        return None, finding, ""
    evidence = citations if old_form.is_marker(source) else source
    draft = plan_row(subject, row.index + 1, texts, run)
    if old_form.is_marker(source):
        draft.refuse(
            drafts.SOURCE_MISSING, f"Citations cell {citations!r} cites lines with no file."
        )
    return draft, finding, evidence


def _cell(row: tables.Row, at: int | None) -> str:
    return row.cells[at].strip() if at is not None and len(row.cells) > at else ""


def _superseded(columns: tuple[str, ...]) -> bool:
    return old_form.FINDING_COLUMN in columns and old_form.SOURCE_PATH_COLUMN in columns


def read_document(document: Path, run: Pass) -> tuple[list[TableDraft], list[Draft]]:
    """Every superseded construct in one document, read once. No file is opened twice."""
    subject = subject_of(document, run.onboarding_root)
    run.result.subjects[subject.relative] = subject.path
    lines = run.sources.lines(document)
    scanned = cells.scan_tables(lines)
    drafted: list[TableDraft] = []
    for header, body in scanned:
        columns = tuple(cell.strip().lower() for cell in header.cells)
        if not _superseded(columns):
            continue
        run.result.tables += 1
        run.result.rows += len(body)
        drafted.append(plan_table(subject, header, body, columns, run))
    occupied = cells.table_lines(lines, scanned)
    return drafted, plan_prose(subject, lines, occupied, run)


# --------------------------------------------------------------------------------------
# PROSE. The superseded spelling is an anchor with a parenthesised `L` range beside it, and
# it is read PER LINE rather than per block: the check joins a paragraph into one string to
# find a construct that wraps, and there is no way back from an offset in a joined block to
# the two lines it straddles. A wrapped one is counted in ``proseNotOnOneLine`` and left.
# --------------------------------------------------------------------------------------


def prose_sites(line: str) -> list[tuple[int, int, str]]:
    """``(start, end, anchor text)`` for each superseded citation written on this one line.

    The same three shapes :mod:`prose` reports, plus the bare two-endpoint range that names
    no anchor at all -- claimed here so it reaches a curator rather than being counted as a
    construct this could not reach.
    """
    spans = inline_scan.code_span_ranges(line)
    found: list[tuple[int, int, str]] = []
    for start, end in prose.anchor_extents(line, spans):
        outside = prose.AFTER_ANCHOR.match(line[end:])
        if outside is not None:
            found.append((start, end + outside.end(), line[start:end]))
            continue
        inside = prose.INSIDE_PARENS.match(line[end:])
        if inside is not None and line[:start].rstrip().endswith("("):
            found.append((len(line[:start].rstrip()) - 1, end + inside.end(), line[start:end]))
    return sorted(found + _bare_sites(line, found))


def _bare_sites(line: str, taken: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """``(L126-L173)`` with nothing beside it. A single number is this repository's leaf
    shorthand as often as a line and is not claimed, exactly as the check does not claim it."""
    return [
        (match.start(), match.end(), "")
        for match in prose.BARE_PARENS.finditer(line)
        if prose.TWO_ENDPOINTS.search(match.group("range"))
        and not any(low <= match.start() < high for low, high, _text in taken)
    ]


def plan_prose(subject: Subject, lines: list[str], occupied: set[int], run: Pass) -> list[Draft]:
    """Every superseded prose citation in one document that sits on a single line."""
    lines = active_claim_lines(lines)
    candidates = [
        (index, line)
        for index, line in inline_scan.unfenced_lines(lines)
        if "(" in line and MAY_CITE.search(line) is not None
    ]
    if not candidates:
        return []
    found: list[Draft] = []
    wrapped = 0
    for index, line in candidates:
        if index in occupied:
            continue
        for site in prose_sites(line):
            if _is_wrapped_tail(lines, index, site):
                wrapped += 1
                continue
            found.append(_plan_prose_site(subject, index + 1, line, site, run))
    run.result.prose_seen += len(found)
    run.result.wrapped += wrapped + _unreachable(lines, occupied, len(found) + wrapped)
    return found


def _is_wrapped_tail(lines: list[str], index: int, site: tuple[int, int, str]) -> bool:
    """Whether this bare range is the second line of an anchored construct that wrapped.

    A backticked anchor ending one line and ``(L4-L5)`` opening the next is ONE citation to
    the check, which joins the paragraph before reading it. Reported here as an unanchored
    range it would tell a curator to add an anchor already sitting one line above.
    """
    start, _end, written = site
    if written or lines[index][:start].strip() or index == 0:
        return False
    above = lines[index - 1].rstrip()
    if not above.endswith(("`", '"', "\u201d")):
        return False
    return bool(prose.anchor_extents(above, inline_scan.code_span_ranges(above)))


def _unreachable(lines: list[str], occupied: set[int], reached: int) -> int:
    """Count joined-paragraph citation sites the per-line rewrite cannot reach."""
    joined = sum(len(prose_sites(block.text)) for block in prose.blocks(lines, occupied))
    return max(0, joined - reached)


def _plan_prose_site(
    subject: Subject, line: int, text: str, site: tuple[int, int, str], run: Pass
) -> Draft:
    """Plan one prose citation against the card's declared path.

    The existing range is trusted only when it contains the anchor. Otherwise the refusal
    distinguishes an anchor elsewhere in that file from one absent from the file entirely.
    """
    start, end, written = site
    draft = Draft(
        subject=subject,
        line=line,
        kind=drafts.PROSE,
        site=Site(line, start, end),
        text=text[start:end].strip(),
    )
    draft.anchors = model.anchors_in(written)[0]
    draft.raw_span = old_form.old_span(text[start:end])
    if not draft.anchors:
        draft.refuse(drafts.PROSE_ANCHOR_MISSING, f"{draft.text!r} names no anchor.")
        return draft
    target = run.trees.resolve(subject.path) if subject.path else None
    if target is None:
        draft.refuse(drafts.PROSE_PATH_UNKNOWN, f"Card path {subject.path!r} resolves to no file.")
        return draft
    draft.paths = (subject.path,)
    draft.hint = verified(draft, run)
    if draft.hint is None:
        draft.refuse(
            drafts.PROSE_ANCHOR_NOT_IN_CITED_RANGE,
            _not_in_range_detail(draft, target, run),
        )
    return draft


# --------------------------------------------------------------------------------------
# PLACEMENT AND WRITING
# --------------------------------------------------------------------------------------


def place(draft: Draft, seen: Sightings, run: Pass) -> str | None:
    """The generated Source list for one draft, or ``None`` when it was declined.

    The resolver is ``--fix``'s, unchanged, fed a citation whose range is the VERIFIED hint
    or nothing at all. With no hint no extent overlaps, so an anchor occurring twice in a
    cited file declines to a curator rather than being picked by position.
    """
    span = draft.hint or old_form.Span(start=0, end=0)
    claim = model.Claim(
        line=draft.line,
        anchors=draft.anchors,
        citations=tuple(
            model.Citation(
                text=_written(path, draft.hint), path=path, start=span.start, end=span.end
            )
            for path in draft.paths
        ),
        malformed=(),
        unchecked_spans=0,
    )
    outcome = repair.plan(claim, run.trees, run.sources, _Sightings(seen))
    if isinstance(outcome, repair.Decline):
        draft.refuse(
            outcome.code,
            outcome.message,
            None if outcome.anchor is None else outcome.anchor.written,
            parsed=parser_dependent(draft),
        )
        return None
    source = _generated(draft, outcome)
    return None if source is None else _scoped(draft, source, run)


class _Sightings(dict[model.Anchor, symbol_index.Sightings]):
    """The located anchors, answering NOWHERE for one that was never looked for.

    An anchor a cited file already holds is deliberately not located, and the resolver never
    reads its entry -- it consults the tree only after every cited file has failed. The empty
    answer keeps that a fact about the data rather than a lookup that must not happen.
    """

    def __missing__(self, anchor: model.Anchor) -> symbol_index.Sightings:
        return symbol_index.Sightings()


def _written(path: str, hint: old_form.Span | None) -> str:
    """How a synthetic citation names itself in a refusal message.

    Never a valid ``path:start-end``, which is what :func:`_generated` reads to tell a
    generated source from one the resolver could only carry across unchanged.
    """
    return path if hint is None else f"{path}:{hint.start}-{hint.end} (verified)"


def _generated(draft: Draft, outcome: repair.Repair) -> str | None:
    """The repair's sources, rejected whole if any cited file yielded no range of its own.

    A CARRIED source is the resolver saying it could not regenerate that one; it is spelled
    as a bare path here because the synthetic citation fed in carries no range. In a repair
    keeping it is right -- the old range still stands. In a migration there is no old range
    to stand, so the row goes to a curator rather than quietly losing a file it cited.
    """
    dropped = [one for one in outcome.sources if not model.SOURCE_PATTERN.fullmatch(one)]
    if not dropped:
        return "; ".join(outcome.sources)
    draft.refuse(
        drafts.SOURCE_HOLDS_NO_ANCHOR,
        f"No anchor of this row occurs in {', '.join(dropped)}.",
        parsed=parser_dependent(draft),
    )
    return None


def _scoped(draft: Draft, source: str, run: Pass) -> str | None:
    """Select the range when generation narrows a verified span.

    Use a generated declaration extent. For a mention, retain the verified block unless it
    spans more than ``VERIFIED_SPAN_FILE_SHARE`` of the file; decline that broad mention for
    curator review. The decision is based on explicit path resolution and verified containment.
    """
    if draft.hint is None or not _narrowed(source, draft.hint) or not _mention_only(draft, run):
        return source
    target = run.trees.resolve(draft.paths[0])
    if target is None:  # pragma: no cover - hint is only set for a path that resolved
        return source
    total = len(run.sources.lines(target))
    span = draft.hint.end - draft.hint.start + 1
    if total and span / total > VERIFIED_SPAN_FILE_SHARE:
        draft.refuse(
            drafts.ANCHOR_NOT_THE_SUBJECT,
            f"The cited range covers {span} of {total} lines in {draft.paths[0]} and holds "
            f"{anchor_cell(draft)} only as a mention, never as a declaration.",
            parsed=True,
        )
        return None
    return f"{draft.paths[0]}:{draft.hint.start}-{draft.hint.end}"


def _narrowed(source: str, hint: old_form.Span) -> bool:
    """Whether every generated range is shorter than the multi-line span it came from."""
    widths = [
        int(one.group("end")) - int(one.group("start")) + 1
        for one in model.SOURCE_PATTERN.finditer(source)
        if one.group("end")
    ]
    return bool(widths) and hint.end > hint.start and max(widths) < hint.end - hint.start + 1


def _mention_only(draft: Draft, run: Pass) -> bool:
    """Whether every extent behind this range is a MENTION rather than a declaration.

    Read back through the same cache the resolver used, so the two cannot disagree about
    what was found. A parsed language with no declaration of the name in the cited file is
    the case this exists for -- tree-sitter cannot declare what the file does not.
    """
    kinds = {
        one.kind
        for anchor in draft.anchors
        for path in draft.paths
        if (target := run.trees.resolve(path)) is not None
        for one in run.sources.view(target, path).extents(anchor)
    }
    return bool(kinds) and extents.DEFINITION not in kinds


def anchor_cell(draft: Draft) -> str:
    return "; ".join(one.written for one in draft.anchors)


def parser_dependent(draft: Draft) -> bool:
    """Whether this draft's RANGE came from a parse rather than from literal matching.

    A heading section and a quoted literal are found by reading the text, so their ranges do
    not move when the parser changes. An identifier's range does: in a parsed language it is
    the construct that binds the name, and everywhere else it is wherever the name appears,
    which is a mention as readily as a declaration. Report the two apart -- a generated wrong
    range looks deliberate once it is written into the tree.
    """
    return any(one.kind == model.SYMBOL for one in draft.anchors)


def unparsed_target(draft: Draft) -> bool:
    """Whether any cited file is one the extent layer cannot parse today."""
    return any(not extents.parsed(path) for path in draft.paths)


def _provenance(draft: Draft, result: Result) -> None:
    """Which of the three ways this draft's range was found, counted apart."""
    if not parser_dependent(draft):
        result.converted_literal += 1
    elif unparsed_target(draft):
        result.converted_occurrence += 1
    else:
        result.converted_declaration += 1


def table_edits(table: TableDraft, seen: Sightings, run: Pass) -> list[tuple[int, str]]:
    """The whole table in the new shape: header, delimiter, and every body row.

    EVERY row is placed before anything is written, so a document's work order is the
    complete offender list rather than the first refusal in each table (L6-R15).
    """
    placed = [
        (draft, finding, evidence, _row_source(draft, seen, run))
        for draft, finding, evidence in table.rows
    ]
    run.result.converted_tables += 1
    written = [(table.header, HEADER_ROW), (table.header + 1, DELIMITER_ROW)]
    for index in range(len(placed)):
        written.append((table.header + 2 + index, _row(table, placed[index], run.result)))
    return written


def _row(table: TableDraft, placed: Placed, result: Result) -> str:
    """One body row at the new width: converted, padded, or carrying its own old evidence."""
    draft, finding, evidence, source = placed
    if draft is None:
        result.placeholders += 1
        return f"| {finding} | {table.marker} | {table.marker} |"
    if source is None:
        result.declined.append(_declined(draft, result))
        return f"| {finding} | {table.marker} | {evidence} |"
    result.converted_rows += 1
    _provenance(draft, result)
    return f"| {finding} | {anchor_cell(draft)} | {source} |"


def _declined(draft: Draft, result: Result) -> work_order.Item:
    """The refusal this draft recorded, counted on the way past."""
    assert draft.decline is not None
    if draft.decline.code == repair.ANCHOR_AMBIGUOUS and draft.raw_span is not None:
        result.hint_would_have_helped += 1
    return draft.decline


def _row_source(draft: Draft | None, seen: Sightings, run: Pass) -> str | None:
    if draft is None or draft.decline is not None:
        return None
    return place(draft, seen, run)


def prose_text(draft: Draft, seen: Sightings, run: Pass) -> str | None:
    """``cit:([anchors], sources)`` for one prose citation, on ONE line by construction.

    A rewriter that emitted a wrapped construct would write something the next pass skips,
    so the joined form is checked rather than assumed.
    """
    source = _row_source(draft, seen, run)
    if source is None:
        return None
    written = f"{prose.CIT_MARK}[{anchor_cell(draft)}], {source})"
    return written if "\n" not in written else None


def live_drafts(drafted: list[TableDraft], found: list[Draft]) -> list[Draft]:
    """Every draft still eligible to convert -- nothing about the row itself refused it."""
    rows = [
        draft for table in drafted for draft, _finding, _evidence in table.rows if draft is not None
    ]
    return [draft for draft in rows + found if draft.decline is None]


def anchors_to_locate(live: list[Draft], run: Pass) -> tuple[model.Anchor, ...]:
    """Only the anchors NO cited file holds -- the ones the tiebreaker will search for.

    The repository-wide lookup is unnecessary for an anchor already sitting in its cited
    file: the resolver queries the shared index only when a cited file holds none of them.
    The test is :class:`extents.FileView`'s, the same extent contract the index materialises.
    """
    return tuple(
        anchor
        for draft in live
        for anchor in draft.anchors
        if not _held_by_a_cited_file(anchor, draft, run)
    )


def _held_by_a_cited_file(anchor: model.Anchor, draft: Draft, run: Pass) -> bool:
    return any(
        run.sources.view(target, path).extents(anchor)
        for path in draft.paths
        if (target := run.trees.resolve(path)) is not None
    )


def migrate_onboarding_root(
    onboarding_root: Path,
    code_repository_root: Path | Trees,
    *,
    dry_run: bool = False,
    only: str | None = None,
    expected_snapshot: str | None = None,
) -> dict[str, Any]:
    """Convert every superseded citation in the memory tree, and report what it would not.

    Every memory document is read once into ``sources``. Every surviving anchor and the
    post-write check query the same immutable source-index lease, so neither performs a
    document-scoped source-tree parse.
    """
    source_index.validate_operation_scope(
        only,
        expected_snapshot,
        leased_index=False,
    )
    trees = operation_trees(onboarding_root, code_repository_root)
    selected = model.documents_in(onboarding_root, only)
    with source_index.open_repository_index(trees, expected_snapshot=expected_snapshot) as index:
        run = Pass(
            onboarding_root=onboarding_root,
            trees=trees,
            index=index,
            sources=Sources(),
            result=Result(),
        )
        drafted: list[TableDraft] = []
        found: list[Draft] = []
        for document in selected:
            run.result.documents += 1
            here, prose_here = read_document(document, run)
            drafted.extend(here)
            found.extend(prose_here)
        live = live_drafts(drafted, found)
        seen = symbol_index.locate(anchors_to_locate(live, run), run.trees, index=index)
        edits = _edits(drafted, found, seen, run)
        run.result.written = len(edits)
        if not dry_run:
            _write(edits)
            run.result.remaining = range_resolution.check_onboarding_root(
                onboarding_root, code_repository_root, only=only, index=index
            )["findingCount"]
        return payload(
            onboarding_root,
            trees.code_root,
            run.result,
            index,
            dry_run=dry_run,
        )


@dataclass
class Edit:
    """One document's pending writes: whole table lines, and spans inside prose lines."""

    lines: dict[int, str] = field(default_factory=dict)
    spans: list[tuple[Site, str]] = field(default_factory=list)


def _edits(
    drafted: list[TableDraft], found: list[Draft], seen: Sightings, run: Pass
) -> dict[Path, Edit]:
    edits: dict[Path, Edit] = {}
    for table in drafted:
        rows = table_edits(table, seen, run)
        edits.setdefault(table.subject.document, Edit()).lines.update(rows)
    for draft in found:
        text = prose_text(draft, seen, run)
        if text is None:
            run.result.declined.append(_declined(draft, run.result))
            continue
        run.result.converted_prose += 1
        _provenance(draft, run.result)
        edits.setdefault(draft.subject.document, Edit()).spans.append((draft.site, text))
    return edits


def _write(edits: dict[Path, Edit]) -> None:
    """Spans first, then whole lines. The two never touch the same line -- a table line is
    kept out of the prose scan -- so neither ordering loses an edit; this one is stated so
    a reader does not have to re-derive that they are disjoint."""
    documents = Documents()
    for document, edit in edits.items():
        lines = rewritten(documents.lines(document), edit.spans)
        for line, text in edit.lines.items():
            lines[line - 1] = text
        document.write_text("\n".join(lines), encoding="utf-8")


def payload(
    onboarding_root: Path,
    code_repository_root: Path,
    result: Result,
    index: source_index.RepositoryIndex,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """The complete conversion list and the complete decline list, never a sample (L6-R15).

    THE TWO HALVES ARE REPORTED APART. A conversion that is pure SHAPE -- the Anchor column,
    the widened delimiter, a padded row, a markdown link become ``path:start-end`` -- is
    final whatever parses the code. A conversion whose RANGE was generated from an identifier
    moves when the extent layer does, and today's layer locates an identifier in an unparsed
    language wherever the name appears rather than where it is declared. Mixing the two into
    one number would publish a provisional figure as a settled one.

    ``findingsRemaining`` is re-measured after a WRITE and is what ``ok`` is read off there.
    A migration reporting success from its own arithmetic would say ok over a tree it had
    half converted, which is the exact state this format change has already produced once.
    A DRY RUN changed nothing, so it re-measures nothing and reports ``null`` rather than a
    number a reader could mistake for the post-migration tree; ``findingsRemeasured`` says
    which of the two happened. ``ok`` is false on a dry run for the same reason -- nothing
    was done, so nothing is done.
    """
    return {
        "ok": not result.declined and not result.remaining and not dry_run,
        "operation": OPERATION,
        "onboardingRoot": onboarding_root.as_posix(),
        "codeRoot": code_repository_root.as_posix(),
        "dryRun": dry_run,
        "documentsScanned": result.documents,
        "supersededTables": result.tables,
        "supersededRows": result.rows,
        "tablesConverted": result.tables,
        "rowsConverted": result.converted_rows,
        "rowsKeepingOldEvidence": result.rows - result.converted_rows - result.placeholders,
        "placeholderRowsPadded": result.placeholders,
        "supersededProseCitations": result.prose_seen,
        "proseConverted": result.converted_prose,
        "proseNotOnOneLine": result.wrapped,
        "documentsWritten": result.written,
        "rangesFromLiteralMatch": result.converted_literal,
        "rangesFromDeclaration": result.converted_declaration,
        "rangesFromOccurrenceOnly": result.converted_occurrence,
        "declinedCount": len(result.declined),
        "declinedByReason": work_order.counted(result.declined),
        "declinedParserDependent": work_order.counted(
            [one for one in result.declined if one.parser_dependent]
        ),
        "ambiguitiesAnOldRangeWouldHavePicked": result.hint_would_have_helped,
        "workOrders": work_order.orders(result.declined, result.subjects),
        "sourceIndex": index.telemetry(post_fix_recheck=not dry_run),
        "findingsRemaining": None if dry_run else result.remaining,
        "findingsRemeasured": not dry_run,
    }
