"""Concurrent append and compaction preserve both provider logs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest
from _store_durability import (
    PROVIDER_CASES,
    run_forced_lost_update,
)


def _describe(result: dict[str, object]) -> str:
    attempted = int(result["attempted"])  # type: ignore[arg-type]
    lost = int(result["lost"])  # type: ignore[arg-type]
    percent = (100.0 * lost / attempted) if attempted else 0.0
    return (
        f"{result['case']} / {result['scenario']}: {lost} of {attempted} records reported "
        f"written were missing afterwards ({percent:.2f}% loss); "
        f"sample={result['lost_sample']} torn_lines={result['torn_lines']} "
        f"reclaim_attempts={result.get('reclaim_attempts')} "
        f"successful_reclaims={result.get('successful_reclaims')} "
        f"stragglers={result['stragglers']}"
    )


class _TempRootTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def case_root(self, scenario: str, case: str) -> Path:
        """Give each provider store its own disposable process harness."""
        return self.tmp / scenario / case / "observer"


class ProviderStoreDurabilityTests(_TempRootTest):
    """A real append racing compaction must never lose accepted records."""

    @pytest.mark.evidence_integration
    def test_no_record_is_lost_when_an_append_races_a_compaction(self) -> None:
        """Force the lost-update window deterministically in both record stores."""
        for case in PROVIDER_CASES:
            with self.subTest(store=case):
                result = run_forced_lost_update(case, self.case_root("forced", case))
                self.assertEqual(result["attempted"], 1, _describe(result))
                self.assertEqual(result["lost"], 0, _describe(result))
                self.assertEqual(result["stragglers"], [], _describe(result))


if __name__ == "__main__":
    unittest.main()
