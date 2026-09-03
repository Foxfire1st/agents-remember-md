"""Regenerate citation ranges from anchors.

Tree-wide mode edits only failing claims. ``--document`` additionally normalizes every
claim in that exact document, including a passing provisional range; other documents
remain byte-identical. Malformed sources, missing anchors, superseded table shapes, and
ambiguous/absent locations are declined with a complete work order.

Wrapped multi-line ``cit:`` constructs are counted in ``claimsNotOnOneLine`` and left
unchanged. The checker runs again after edits and reports all remaining findings. Rewrites
preserve every byte outside the selected citation span.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from agents_remember.memory_quality.integrity.onboarding_drift_check.discovery import rel
from agents_remember.memory_quality.style.citations import (
    cells,
    deterministic_projection,
    drafts,
    migration,
    model,
    old_form,
    prose,
    range_resolution,
    repair,
    source_index,
    symbol_index,
)
from agents_remember.memory_quality.style.citations.editing import Documents, Site, rewritten
from agents_remember.memory_quality.style.citations.range_resolution import (
    ClaimScope,
    Resolved,
    Sources,
)
from agents_remember.memory_quality.style.citations.resolution import Trees, operation_trees
from agents_remember.memory_quality.style.document_shape import inline_scan

OPERATION = "citation_fix"


@dataclass(frozen=True)
class Candidate:
    """One gating claim, or one passing claim selected for scoped normalisation."""

    document: Path
    relative: str
    claim: model.Claim
    site: Site
    repairing: bool
    gating: bool


@dataclass(frozen=True)
class Applied:
    """One claim's source list, before and after."""

    path: str
    line: int
    was: str
    now: str
    repairing: bool


@dataclass(frozen=True)
class Refused:
    """One claim ``--fix`` left for the curator agent, with the facts it needs."""

    path: str
    line: int
    code: str
    anchor: str | None
    message: str

    @classmethod
    def from_repair(cls, path: str, line: int, decline: repair.Decline) -> Refused:
        return cls(
            path=path,
            line=line,
            code=decline.code,
            anchor=None if decline.anchor is None else decline.anchor.written,
            message=decline.message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "code": self.code,
            "anchor": self.anchor,
            "message": self.message,
        }


@dataclass
class Result:
    """What one run of the fixer did, and everything it refused."""

    applied: list[Applied] = field(default_factory=list)
    refused: list[Refused] = field(default_factory=list)
    documents: int = 0
    candidates: int = 0
    wrapped: int = 0
    written: int = 0
    remaining: int = 0
    projections: list[deterministic_projection.Projection] = field(default_factory=list)


def table_sites(lines: list[str]) -> list[tuple[model.Claim, Site]]:
    """Every conforming table row, with the span of its Source cell."""
    found: list[tuple[model.Claim, Site]] = []
    for table in cells.citation_tables(lines):
        if not table.conforming:
            continue
        at = table.columns.index(cells.SOURCE_COLUMN)
        for claim in table.rows:
            start, end = inline_scan.cell_spans(lines[claim.line - 1])[at]
            found.append((claim, Site(line=claim.line, start=start, end=end)))
    return found


def prose_sites(lines: list[str]) -> tuple[list[tuple[model.Claim, Site]], int]:
    """Every ``cit:`` that opens and closes on ONE line, and the count of those that do not.

    A wrapped construct is skipped rather than joined: the check reads prose as blocks with
    the line breaks removed, and there is no way back from an offset in a joined block to
    the two lines it straddles without re-deriving the join.
    """
    occupied = cells.table_lines(lines)
    found: list[tuple[model.Claim, Site]] = []
    wrapped = 0
    for index, line in inline_scan.unfenced_lines(lines):
        if index in occupied:
            continue
        for opened, closed in cit_bounds(line):
            if closed is None:
                wrapped += 1
                continue
            site = prose_site(line, index + 1, opened, closed)
            claim = prose.parse_citation(line[opened + 1 : closed], index + 1)
            if site is not None and claim is not None:
                found.append((claim, site))
    return found, wrapped


def cit_bounds(line: str) -> list[tuple[int, int | None]]:
    """Each ``cit:`` opening parenthesis on the line, and its closer when there is one."""
    spans = inline_scan.code_span_ranges(line)
    found: list[tuple[int, int | None]] = []
    at = line.find(prose.CIT_MARK)
    while at >= 0:
        if inline_scan.enclosing_span_end(at, spans) is None:
            opened = at + len(prose.CIT_MARK) - 1
            found.append((opened, model.matching(line, opened, spans, prose.PAREN_CLOSE)))
        at = line.find(prose.CIT_MARK, at + len(prose.CIT_MARK))
    return found


def prose_site(line: str, at: int, opened: int, closed: int) -> Site | None:
    found = prose.parts(line[opened + 1 : closed])
    if found is None:
        return None
    return Site(line=at, start=opened + 1 + found.sources, end=closed)


def sites(lines: list[str]) -> tuple[list[tuple[model.Claim, Site]], int]:
    """Every claim in one document whose source list can be rewritten in place."""
    found, wrapped = prose_sites(lines)
    return table_sites(lines) + found, wrapped


def scope_of(relative: str, claim: model.Claim, trees: Trees, sources: Sources) -> ClaimScope:
    resolved = [
        Resolved(citation=citation, file=target)
        for citation in claim.citations
        if (target := trees.resolve(citation.path)) is not None
    ]
    return ClaimScope(document=relative, claim=claim, resolved=tuple(resolved), sources=sources)


def failing(scope: ClaimScope, trees: Trees) -> bool:
    """Whether this claim carries a defect a regenerated range could clear."""
    if range_resolution.out_of_bounds(scope) or range_resolution.unsatisfied(scope):
        return True
    return any(
        trees.resolve(citation.path) is None and trees.ours(citation.path)
        for citation in scope.claim.citations
    )


@dataclass
class Walk:
    """Run-scoped trees, sources, documents, and result for one candidate walk."""

    trees: Trees
    index: source_index.RepositoryIndex
    sources: Sources
    documents: Documents
    result: Result


def candidates(
    onboarding_root: Path, walk: Walk, documents: list[Path], *, scoped: bool
) -> list[Candidate]:
    """Every repairable or duplicate-bearing claim, plus scoped passing claims.

    A malformed Source segment excludes the WHOLE claim. Replacing a structured cell from
    only its parsed citations would silently delete the malformed evidence and make the
    subsequent check green by data loss. The checker keeps reporting it for curator action.
    """
    found: list[Candidate] = []
    for document in documents:
        walk.result.documents += 1
        lines = walk.documents.lines(document)
        relative = rel(document, onboarding_root)
        located, wrapped = sites(lines)
        walk.result.wrapped += wrapped
        for claim, site in located:
            if claim.malformed:
                continue
            scope = scope_of(relative, claim, walk.trees, walk.sources)
            repairing = failing(scope, walk.trees)
            duplicate = bool(range_resolution.repeated_sources(claim))
            normalisable = (scoped and bool(scope.resolved)) or duplicate
            if claim.anchors and claim.citations and (repairing or normalisable):
                found.append(
                    Candidate(
                        document=document,
                        relative=relative,
                        claim=claim,
                        site=site,
                        repairing=repairing,
                        gating=repairing or duplicate,
                    )
                )
    walk.result.candidates = sum(one.gating for one in found)
    return found


@dataclass
class Staging:
    """One run's pending writes: per-document edit batches, history bullets, and stamp."""

    edits: dict[Path, list[tuple[Site, str]]] = field(default_factory=dict)
    bullets: dict[Path, list[str]] = field(default_factory=dict)
    stamp: datetime | None = None


def fix_onboarding_root(
    onboarding_root: Path,
    code_repository_root: Path | Trees,
    *,
    dry_run: bool = False,
    only: str | None = None,
    expected_snapshot: str | None = None,
) -> dict[str, Any]:
    """Regenerate every repairable range in the memory tree, and report the rest.

    THE CALLER OWNS THE WRITE GUARD. This function writes wherever it is pointed; that
    ``onboarding_root`` is a leaf's memory worktree and never the official memory repo is
    established before the call, by the enclosure contract.
    """
    source_index.validate_operation_scope(
        only,
        expected_snapshot,
        leased_index=False,
    )
    trees = operation_trees(onboarding_root, code_repository_root)
    selected = model.documents_in(onboarding_root, only)
    with source_index.open_repository_index(trees, expected_snapshot=expected_snapshot) as index:
        sources = Sources()
        document_cache = Documents()
        result = Result()
        walk = Walk(
            trees=trees,
            index=index,
            sources=sources,
            documents=document_cache,
            result=result,
        )
        found = candidates(onboarding_root, walk, selected, scoped=only is not None)
        seen = symbol_index.locate(
            tuple(anchor for one in found if one.repairing for anchor in one.claim.anchors),
            trees,
            index=index,
        )
        staging = Staging()
        if staging.stamp is None:
            staging.stamp = deterministic_projection.now_utc()
        for one in found:
            outcome = repair.plan(one.claim, trees, sources, seen) if one.repairing else None
            _decide(one, outcome, walk, staging, onboarding_root)
        for document, doc_bullets in staging.bullets.items():
            lines = document_cache.lines(document)
            heading = deterministic_projection.history_section_line(lines)
            if heading is None:  # pragma: no cover - bullets only stage beside a section
                continue
            staging.edits.setdefault(document, []).append(
                deterministic_projection.history_edit(lines, heading, doc_bullets)
            )
        for document, per_document in staging.edits.items():
            digest = deterministic_projection.document_digest(
                document_cache.lines(document), per_document
            )
            relative = rel(document, onboarding_root)
            for position, projection in enumerate(walk.result.projections):
                if projection.document == relative:
                    walk.result.projections[position] = replace(
                        projection, new_document_digest=digest
                    )
        result.written = len(staging.edits)
        if not dry_run:
            for document, per_document in staging.edits.items():
                body = "\n".join(rewritten(document_cache.lines(document), per_document))
                document.write_text(body, encoding="utf-8")
        result.remaining = range_resolution.check_onboarding_root(
            onboarding_root, code_repository_root, only=only, index=index
        )["findingCount"]
        return payload(
            onboarding_root,
            trees.code_root,
            result,
            index,
            dry_run=dry_run,
        )


def _decide(
    one: Candidate,
    outcome: repair.Repair | repair.Decline | None,
    walk: Walk,
    staging: Staging,
    onboarding_root: Path,
) -> None:
    line = walk.documents.lines(one.document)[one.site.line - 1]
    was = line[one.site.start : one.site.end].strip()
    if one.repairing:
        assert outcome is not None
        if isinstance(outcome, repair.Decline):
            walk.result.refused.append(Refused.from_repair(one.relative, one.claim.line, outcome))
            return
        now = "; ".join(dict.fromkeys(outcome.sources))
    else:
        now, refused = _scoped_source(one, onboarding_root, walk)
        if refused is not None:
            walk.result.refused.append(refused)
            return
        assert now is not None
    if now == was:
        return
    walk.result.applied.append(
        Applied(
            path=one.relative,
            line=one.claim.line,
            was=was,
            now=now,
            repairing=one.gating,
        )
    )
    staging.edits.setdefault(one.document, []).append((one.site, now))
    if not one.repairing:
        return
    assert isinstance(outcome, repair.Repair)
    lines = walk.documents.lines(one.document)
    heading = deterministic_projection.history_section_line(lines)
    stamp = staging.stamp if staging.stamp is not None else deterministic_projection.now_utc()
    projection = deterministic_projection.plan_projection(
        deterministic_projection.ProjectionRequest(
            lines=lines,
            site=one.site,
            relative=one.relative,
            claim=one.claim,
            outcome=outcome,
            index=walk.index,
            now=stamp,
            history_line=heading,
        )
    )
    if isinstance(projection, deterministic_projection.ProjectionDecline):
        walk.result.refused.append(
            Refused(
                path=one.relative,
                line=one.claim.line,
                code=projection.code,
                anchor=projection.anchor,
                message=projection.message,
            )
        )
        return
    walk.result.projections.append(projection)
    if heading is not None:
        bullet = projection.history_bullet
        assert bullet is not None
        staging.bullets.setdefault(one.document, []).append(bullet)


def _scoped_source(
    one: Candidate,
    onboarding_root: Path,
    walk: Walk,
) -> tuple[str | None, Refused | None]:
    """Normalise each citation, exact-deduplicate it, and preserve verified spans.

    Reuse ``migration._scoped`` per citation so pooled sources cannot erase another source's
    verified hint. Declaration extents, verified mention blocks, and over-broad mention
    refusals therefore share one contract in migration and steady-state formatting.
    """
    run = migration.Pass(
        onboarding_root=onboarding_root,
        trees=walk.trees,
        index=walk.index,
        sources=walk.sources,
        result=drafts.Result(),
    )
    written: list[str] = []
    seen: set[str] = set()
    for citation in one.claim.citations:
        source, refused = _scoped_citation(one, citation, run)
        if refused is not None:
            return None, refused
        for generated in model.citations_in(source)[0]:
            if generated.text in seen:
                continue
            seen.add(generated.text)
            written.append(generated.text)
    return "; ".join(written), None


def _scoped_citation(
    one: Candidate,
    citation: model.Citation,
    run: migration.Pass,
) -> tuple[str, Refused | None]:
    """One original Source segment, generated only from anchors that segment verifies."""
    target = run.trees.resolve(citation.path)
    if target is None:
        return citation.text, None
    body = run.sources.body(target, citation.start, citation.end)
    anchors = tuple(anchor for anchor in one.claim.anchors if model.occurs_in(anchor, body))
    if not anchors:
        return citation.text, None
    claim = model.Claim(
        line=one.claim.line,
        anchors=anchors,
        citations=(citation,),
        malformed=(),
        unchecked_spans=0,
    )
    unseen = {anchor: symbol_index.Sightings() for anchor in anchors}
    outcome = repair.plan(claim, run.trees, run.sources, unseen)
    if isinstance(outcome, repair.Decline):
        return citation.text, Refused.from_repair(one.relative, one.claim.line, outcome)
    generated = "; ".join(outcome.sources)
    hint = old_form.verified_hint(
        anchors,
        old_form.Span(start=citation.start, end=citation.end),
        run.sources.lines(target),
    )
    draft = drafts.Draft(
        subject=drafts.Subject(
            document=one.document,
            relative=one.relative,
            path=citation.path,
            repository="",
        ),
        line=one.claim.line,
        kind=drafts.TABLE_ROW,
        site=one.site,
        text=generated,
        anchors=anchors,
        paths=(citation.path,),
        hint=hint,
    )
    scoped = migration._scoped(draft, generated, run)
    if scoped is not None:
        return scoped, None
    declined = draft.decline
    assert declined is not None
    return citation.text, Refused(
        path=one.relative,
        line=one.claim.line,
        code=declined.code,
        anchor=declined.anchor,
        message=f"{declined.message} {declined.action}",
    )


def payload(
    onboarding_root: Path,
    code_repository_root: Path,
    result: Result,
    index: source_index.RepositoryIndex,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """The complete offender list and the complete repair list, never a sample (L6-R15).

    ``findingsRemaining`` is re-measured after the write, and it is what ``ok`` is read off.
    A fixer that reported success from its own refusals alone would say ok on a tree still
    holding thousands of findings it never had a rule for -- a halfway state indistinguishable
    from a finished one. On a dry run nothing was written, so the number is the tree as it
    stands.
    """
    return {
        "ok": not result.refused and not result.remaining,
        "operation": OPERATION,
        "onboardingRoot": onboarding_root.as_posix(),
        "codeRoot": code_repository_root.as_posix(),
        "dryRun": dry_run,
        "documentsScanned": result.documents,
        "failingClaims": result.candidates,
        "claimsRepaired": sum(one.repairing for one in result.applied),
        "claimsNormalised": sum(not one.repairing for one in result.applied),
        "documentsWritten": result.written,
        "claimsNotOnOneLine": result.wrapped,
        "sourceIndex": index.telemetry(post_fix_recheck=True),
        "repairs": [
            {
                "path": one.path,
                "line": one.line,
                "was": one.was,
                "now": one.now,
                "kind": "repair" if one.repairing else "normalise",
            }
            for one in result.applied
        ],
        "projectionCount": len(result.projections),
        "projections": [one.to_dict() for one in result.projections],
        "repairToolVersion": deterministic_projection.REPAIR_TOOL_VERSION,
        "declinedCount": len(result.refused),
        "declined": [one.to_dict() for one in result.refused],
        "findingsRemaining": result.remaining,
    }
