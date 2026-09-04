"""Deterministic per-category span reduction for R16 telemetry spans.

The analyzer classifies every measured span into exactly one closed category
(the R16 TelemetrySpanKind vocabulary), unions overlapping wall intervals
inside each category so wall time is never double counted, and reduces the
whole export to gross wall and active time.  Arithmetic is reproducible: a
caller can independently recompute every union from the raw span intervals.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from agents_remember.certification.digests import content_digest
from agents_remember.certification.replay.models import (
    SpanCategoryTotals,
    SpanReduction,
)
from agents_remember.certification.telemetry.models import (
    TelemetrySpan,
    TelemetrySpanKind,
)


def category_wall_union_millis(
    spans: Sequence[TelemetrySpan],
    category: TelemetrySpanKind,
) -> int:
    """Union wall milliseconds across the spans of exactly one category."""
    return _wall_union_millis(_interval(item) for item in spans if item.spanKind == category)


def gross_wall_union_millis(spans: Sequence[TelemetrySpan]) -> int:
    """Union wall milliseconds across every measured span (no double count)."""
    return _wall_union_millis(_interval(item) for item in spans)


def analyze_span_categories(spans: Sequence[TelemetrySpan]) -> SpanReduction:
    """Reduce one measured span export into the closed per-category record."""
    ordered = sorted(spans, key=lambda item: (item.spanKind, item.startedEpochMillis))
    by_category: dict[str, list[TelemetrySpan]] = defaultdict(list)
    for item in ordered:
        by_category[item.spanKind].append(item)
    totals: list[SpanCategoryTotals] = []
    for category in sorted(TelemetrySpanKind.__args__):  # type: ignore[attr-defined]
        member_spans = by_category.get(category, [])
        totals.append(
            SpanCategoryTotals(
                category=category,
                wallMillis=category_wall_union_millis(member_spans, category),
                activeMillis=sum(item.activeMillis for item in member_spans),
                spanCount=len(member_spans),
            )
        )
    total_active = sum(item.activeMillis for item in ordered)
    payload = {
        "categories": [item.model_dump(mode="json") for item in totals],
        "grossWallMillis": gross_wall_union_millis(ordered),
        "grossActiveMillis": total_active,
        "spanCount": len(ordered),
    }
    reduction = SpanReduction(
        categories=tuple(totals),
        grossWallMillis=payload["grossWallMillis"],
        grossActiveMillis=payload["grossActiveMillis"],
        spanCount=len(ordered),
        reductionDigest=content_digest(
            {"schemaVersion": "measured-replay-span-reduction/v1", **payload}
        ),
    )
    return reduction


def _interval(span: TelemetrySpan) -> tuple[int, int]:
    return (span.startedEpochMillis, span.startedEpochMillis + span.wallMillis)


def _wall_union_millis(intervals: Iterable[tuple[int, int]]) -> int:
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    total = 0
    cursor: int | None = None
    for start, end in ordered:
        if cursor is None or start >= cursor:
            total += end - start
            cursor = end
        elif end > cursor:
            total += end - cursor
            cursor = end
    return total


__all__ = [
    "SpanCategoryTotals",
    "SpanReduction",
    "TelemetrySpan",
    "TelemetrySpanKind",
    "analyze_span_categories",
    "category_wall_union_millis",
    "gross_wall_union_millis",
]
