"""Tests for the CLI trusted-settings discovery (260703 L1)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.cli.discovery import (
    ConfigDiscoveryError,
    discover_config,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _settings(path: Path, coordination_root: Path) -> Path:
    """A minimal USABLE settings file: coordinationRoot must exist and be absolute."""
    coordination_root.mkdir(parents=True, exist_ok=True)
    return _write(
        path, json.dumps({"version": 1, "coordinationRoot": coordination_root.as_posix()})
    )


def _mcp_json(directory: Path, config_path: Path | None, server: str = "agents-remember") -> Path:
    args = ["--config", config_path.as_posix()] if config_path else []
    return _write(
        directory / ".mcp.json",
        json.dumps({"mcpServers": {server: {"command": "uvx", "args": args}}}),
    )


class DiscoverConfigTests(unittest.TestCase):
    def test_convention_wins_over_registration_in_the_same_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            convention = _settings(
                root / ".claude/mcp/agents-remember-settings.json", root / "ar-coordination"
            )
            other = _settings(root / "other-settings.json", root / "ar-coordination")
            _mcp_json(root, other)
            self.assertEqual(discover_config(root), convention)

    def test_nearest_directory_wins_across_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _settings(root / ".claude/mcp/agents-remember-settings.json", root / "ar-coordination")
            near_settings = _settings(root / "ws" / "elsewhere.json", root / "ar-coordination")
            _mcp_json(root / "ws", near_settings)
            start = root / "ws" / "repo"
            start.mkdir(parents=True)
            self.assertEqual(discover_config(start), near_settings)

    def test_malformed_and_foreign_registrations_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(
                root / ".claude/mcp/agents-remember-settings.json", root / "ar-coordination"
            )
            mid = root / "mid"
            _write(mid / ".mcp.json", "{not json")
            low = mid / "low"
            _mcp_json(low, None)  # agents-remember entry without --config
            _write(
                low / "other" / ".mcp.json",
                json.dumps({"mcpServers": {"someone-else": {"args": ["--config", "/x.json"]}}}),
            )
            start = low / "other"
            self.assertEqual(discover_config(start), settings)

    def test_miss_raises_with_both_patterns_and_the_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = Path(tmp) / "nowhere"
            start.mkdir()
            with self.assertRaises(ConfigDiscoveryError) as ctx:
                discover_config(start)
            message = str(ctx.exception)
            self.assertIn(".claude/mcp/agents-remember-settings.json", message)
            self.assertIn(".mcp.json", message)
            self.assertIn(start.resolve().as_posix(), message)


if __name__ == "__main__":
    unittest.main()
