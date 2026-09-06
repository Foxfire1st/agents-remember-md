"""Actual fixer publication: accepted batches, refusal isolation, and observed conflicts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from agents_remember.memory_quality.style.citations import (
    deterministic_projection as projection,
)
from agents_remember.memory_quality.style.citations import (
    fixer,
    source_index,
)
from agents_remember.memory_quality.style.citations.documents import (
    transaction as document_transaction,
)
from agents_remember.memory_quality.style.citations.resolution import Trees

STAMP = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
HISTORY = "\n## Update History\n\n- 2026-08-01T00:00:00+00:00: Original.\n"
DECLINED = "| Pooled. | `persist` | src/left.py:1-2; src/right.py:99-99 |"
MOVED = "| Single. | `unique` | src/gone.py:1-2 |"
SECOND = "| Second. | `another` | src/missing.py:1-2 |"


@dataclass(frozen=True)
class Scenario:
    code: Path
    onboarding: Path

    def card(self, name: str, *rows: str) -> Path:
        path = self.onboarding / name
        path.write_text(
            "# Citation card\n\n| Finding | Anchor | Source |\n| --- | --- | --- |\n"
            + "\n".join(rows)
            + "\n"
            + HISTORY,
            encoding="utf-8",
        )
        return path

    def fix(self, **options: Any) -> dict[str, Any]:
        with mock.patch.object(projection, "now_utc", return_value=STAMP):
            return fixer.fix_onboarding_root(self.onboarding, self.code, **options)

    def snapshot(self) -> str:
        trees = Trees(code_root=self.code, memory_root=self.onboarding.parent)
        with source_index.open_repository_index(trees) as index:
            return index.snapshot_id


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    code = tmp_path / "code"
    # A real top-level source directory makes absent members local move candidates.
    (code / "src").mkdir(parents=True)
    onboarding = tmp_path / "memory" / "onboarding"
    onboarding.mkdir(parents=True)
    for name, symbol in (
        ("src/left.py", "persist"),
        ("src/right.py", "persist"),
        ("src/current.py", "unique"),
        ("src/second.py", "another"),
    ):
        (code / name).write_text(f"def {symbol}():\n    return 1\n", encoding="utf-8")
    return Scenario(code, onboarding)


def test_mixed_claims_publish_only_the_accepted_edit_and_history(scenario: Scenario) -> None:
    card = scenario.card("mixed.md", DECLINED, MOVED)
    result = scenario.fix()
    after = card.read_bytes()
    assert DECLINED.encode() in after
    assert b"| Single. | `unique` | src/current.py:1-2 |" in after
    assert after.count(b"Generated citation repair:") == 1
    assert b"`persist` repointed" not in after
    assert result["documentsWritten"] == result["claimsRepaired"] == result["projectionCount"] == 1
    assert result["declinedCount"] == 1
    assert result["projections"][0]["newDocumentDigest"] == sha256(after).hexdigest()
    assert result["repairs"][0]["was"] == "src/gone.py:1-2"
    again = scenario.fix()
    assert card.read_bytes() == after
    assert again["repairs"] == again["projections"] == []
    assert again["documentsWritten"] == 0
    assert again["declinedCount"] == 1


def test_preview_digest_matches_later_publication_and_binds_the_complete_batch(
    scenario: Scenario,
) -> None:
    card = scenario.card("accepted.md", MOVED, SECOND)
    before = card.read_bytes()
    preview = scenario.fix(dry_run=True)
    assert card.read_bytes() == before
    assert preview["documentsWritten"] == 0
    assert preview["projectionCount"] == preview["claimsRepaired"] == 2
    applied = scenario.fix()
    assert applied["projections"] == preview["projections"]
    assert applied["documentsWritten"] == 1
    assert all(
        p["newDocumentDigest"] == sha256(card.read_bytes()).hexdigest()
        for p in applied["projections"]
    )
    assert card.read_bytes().count(b"Generated citation repair:") == 2


def test_two_prose_source_cells_on_one_line_keep_their_original_offsets(scenario: Scenario) -> None:
    prose = (
        "First cit:([`unique`], src/gone.py:1-2) and second "
        "cit:([`another`], src/missing.py:1-2) remain distinct."
    )
    card = scenario.card("prose.md", prose)
    result = scenario.fix()
    expected = prose.replace("src/gone.py:1-2", "src/current.py:1-2").replace(
        "src/missing.py:1-2", "src/second.py:1-2"
    )
    assert expected in card.read_text()
    assert result["claimsRepaired"] == result["projectionCount"] == 2
    assert result["documentsWritten"] == 1
    assert result["ok"] is True


CHANGES: dict[str, Callable[[bytes], bytes | None]] = {
    "unrelated body": lambda body: body.replace(b"# Citation card", b"# Concurrent title"),
    "source cell": lambda body: body.replace(b"src/gone.py:1-2", b"src/other.py:7-8"),
    "cell padding": lambda body: body.replace(b" src/gone.py:1-2 ", b"  src/gone.py:1-2  "),
    "history": lambda body: body.replace(b"Original.", b"Concurrent history."),
    "truncated": lambda body: b"# Concurrent replacement\n",
    "deleted": lambda body: None,
}


@pytest.mark.parametrize("change", CHANGES)
def test_observed_conflict_refuses_the_whole_document_and_preserves_other_batches(
    scenario: Scenario, change: str
) -> None:
    conflicted = scenario.card("conflicted.md", MOVED, SECOND)
    independent = scenario.card("independent.md", MOVED)
    concurrent = CHANGES[change](conflicted.read_bytes())
    plan = projection.plan_projection

    def interleave(
        request: projection.ProjectionRequest,
    ) -> projection.Projection | projection.ProjectionDecline:
        outcome = plan(request)
        if request.relative == conflicted.name:
            if concurrent is None:
                conflicted.unlink(missing_ok=True)
            else:
                conflicted.write_bytes(concurrent)
        return outcome

    with mock.patch.object(projection, "plan_projection", side_effect=interleave):
        result = scenario.fix()
    assert (conflicted.read_bytes() if conflicted.exists() else None) == concurrent
    assert result["documentsWritten"] == result["projectionCount"] == result["claimsRepaired"] == 1
    assert result["declinedCount"] == 2
    assert {d["path"] for d in result["declined"]} == {conflicted.name}
    assert {d["code"] for d in result["declined"]} == {projection.PROJECTION_CONFLICT}
    assert {p["document"] for p in result["projections"]} == {independent.name}
    assert {r["path"] for r in result["repairs"]} == {independent.name}
    assert (
        result["projections"][0]["newDocumentDigest"]
        == sha256(independent.read_bytes()).hexdigest()
    )
    assert result["ok"] is False


def test_atomic_replace_failure_keeps_the_original_document(scenario: Scenario) -> None:
    card = scenario.card("failure.md", MOVED)
    before = card.read_bytes()
    atomic_write = document_transaction.atomic_write_bytes

    def fail_at_replace(path: Path, body: bytes) -> None:
        with mock.patch(
            "agents_remember.kernel.atomic_write.os.replace", side_effect=OSError("failed")
        ):
            atomic_write(path, body)

    with (
        mock.patch.object(
            document_transaction, "atomic_write_bytes", side_effect=fail_at_replace
        ) as writer,
        pytest.raises(OSError, match="failed"),
    ):
        scenario.fix()
    writer.assert_called_once()
    assert writer.call_args.args[0] == card
    assert card.read_bytes() == before
    assert list(scenario.onboarding.iterdir()) == [card]


def test_crlf_document_bytes_and_history_line_endings_survive(scenario: Scenario) -> None:
    card = scenario.card("crlf.md", MOVED)
    original = card.read_bytes().replace(b"\n", b"\r\n")
    card.write_bytes(original)
    result = scenario.fix()
    after = card.read_bytes()
    assert b"\n" not in after.replace(b"\r\n", b"")
    assert b"| Single. | `unique` | src/current.py:1-2 |\r\n" in after
    assert HISTORY.replace("\n", "\r\n").split("\r\n\r\n")[1].encode() in after
    assert result["projections"][0]["newDocumentDigest"] == sha256(after).hexdigest()
    assert scenario.fix()["documentsWritten"] == 0


def test_stale_explicit_snapshot_refuses_the_actual_scoped_fixer(scenario: Scenario) -> None:
    card = scenario.card("snapshot.md", MOVED)
    original = card.read_bytes()
    trees = Trees(code_root=scenario.code, memory_root=scenario.onboarding.parent)
    with source_index.open_repository_index(trees) as index:
        expected = index.snapshot_id
    (scenario.code / "src/current.py").write_text("def unique():\n    return 2\n")
    with source_index.open_repository_index(trees) as index:
        assert index.snapshot_id != expected
    with pytest.raises(source_index.SourceIndexError):
        scenario.fix(only=card.name, expected_snapshot=expected)
    assert card.read_bytes() == original
