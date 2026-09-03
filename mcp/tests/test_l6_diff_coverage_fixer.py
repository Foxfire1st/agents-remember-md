"""L6 closeout coverage tests for citation fixer refusal branches."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.memory_quality.style.citations import fixer, model, repair
from agents_remember.memory_quality.style.citations.fixer import Refused, _decide, _scoped_source
from agents_remember.memory_quality.style.citations.model import Citation


def _citation(path: str = "a.py") -> Citation:
    return Citation(text=f"{path}:1-1", path=path, start=1, end=1)


def _one(**over: object) -> SimpleNamespace:
    base = {
        "repairing": False,
        "gating": False,
        "relative": "a.py.md",
        "claim": SimpleNamespace(
            line=1, anchors=(model.Anchor(kind=model.SYMBOL, text="f"),), citations=(_citation(),)
        ),
        "site": SimpleNamespace(line=1, start=0, end=3),
        "document": "doc",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _walk() -> SimpleNamespace:
    return SimpleNamespace(
        documents=SimpleNamespace(lines=lambda document: ["abc"]),
        result=SimpleNamespace(refused=[], applied=[]),
    )


class TestDecideRefused:
    def test_scoped_source_refusal(self) -> None:
        walk = _walk()
        one = _one()
        refused = Refused("a.py.md", 1, "code", "anchor", "msg")
        with mock.patch.object(fixer, "_scoped_source", return_value=(None, refused)):
            _decide(
                cast(fixer.Candidate, one),
                None,
                cast(fixer.Walk, walk),
                fixer.Staging(),
                Path("/onboarding"),
            )
        assert walk.result.refused == [refused]


class TestScopedSourceRefusal:
    def test_citation_refusal(self) -> None:
        walk = _walk()
        walk.trees = SimpleNamespace()
        walk.index = SimpleNamespace()
        walk.sources = SimpleNamespace()
        one = _one()
        refused = Refused("a.py.md", 1, "code", "anchor", "msg")
        with (
            mock.patch.object(fixer.migration, "Pass", return_value=SimpleNamespace()),
            mock.patch.object(fixer, "_scoped_citation", return_value=(None, refused)),
        ):
            source, returned = _scoped_source(
                cast(fixer.Candidate, one), Path("/onboarding"), cast(fixer.Walk, walk)
            )
        assert source is None and returned is refused


class TestScopedCitationRefusals:
    def test_repair_decline(self) -> None:
        one = _one()
        run = SimpleNamespace(
            trees=SimpleNamespace(resolve=lambda path: Path("/x")),
            sources=SimpleNamespace(
                body=lambda target, start, end: "def f():\n    pass\n",
                lines=lambda target: ["def f():\n", "    pass\n"],
            ),
        )
        decline = repair.Decline(code=repair.ANCHOR_ABSENT, anchor=None, detail="gone")
        with mock.patch.object(fixer.repair, "plan", return_value=decline):
            _text, refused = fixer._scoped_citation(
                cast(fixer.Candidate, one), _citation(), cast(fixer.migration.Pass, run)
            )
        assert refused is not None and "gone" in refused.message

    def test_scoped_decline(self) -> None:
        one = _one()
        run = SimpleNamespace(
            trees=SimpleNamespace(resolve=lambda path: Path("/x")),
            sources=SimpleNamespace(
                body=lambda target, start, end: "def f():\n    pass\n",
                lines=lambda target: ["def f():\n", "    pass\n"],
            ),
        )
        outcome = SimpleNamespace(sources=("a.py:1-1",))
        draft = SimpleNamespace(
            decline=SimpleNamespace(code="draft-decline", anchor="f", message="m", action="a")
        )
        with (
            mock.patch.object(fixer.repair, "plan", return_value=outcome),
            mock.patch.object(fixer.old_form, "verified_hint", return_value=None),
            mock.patch.object(fixer.drafts, "Draft", return_value=draft),
            mock.patch.object(fixer.migration, "_scoped", return_value=None),
        ):
            _text, refused = fixer._scoped_citation(
                cast(fixer.Candidate, one), _citation(), cast(fixer.migration.Pass, run)
            )
        assert refused is not None and refused.code == "draft-decline"
