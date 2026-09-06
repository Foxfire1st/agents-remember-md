"""Regression coverage for leaf-local checkout coordination isolation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from agents_remember.controlplane.durable_store import (
    OPERATOR_INBOX_OWNERSHIP,
    append_line,
    declare_process_role,
    declared_process_role,
    exclusive_access,
    rewrite_lines,
)
from agents_remember.kernel.primitives import checkout_coordination
from agents_remember.kernel.primitives.checkout_coordination import (
    CheckoutCoordinationError,
    declare_lifecycle_operation_process,
    declare_test_process,
    declared_daemon_role,
    declared_execution_mode,
    resolve_checkout_location,
)
from agents_remember.kernel.primitives.runtime_config import ConfigError, load_config
from agents_remember_test_support.testing.global_state import preserve_owned_mutable_state


class CheckoutCoordinationIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _checkout(self, *, linked: bool = True) -> tuple[Path, Path]:
        checkout = self.root / "leaf-enclosure" / "candidate-checkout"
        source = (
            checkout
            / "mcp"
            / "src"
            / "agents_remember"
            / "kernel"
            / "primitives"
            / "checkout_coordination.py"
        )
        source.parent.mkdir(parents=True)
        source.touch()
        (checkout / "mcp" / "pyproject.toml").touch()
        marker = checkout / ".git"
        if linked:
            marker.write_text("gitdir: /tmp/example\n", encoding="utf-8")
        else:
            marker.mkdir()
        return checkout, source

    def _settings(self, path: Path) -> None:
        payload = {
            "coordinationRoot": str(self.root / "live-ar-coordination"),
            "workspaceRoot": str(self.root / "workspace"),
            "repositories": {"agents-remember": {}},
            "providers": {},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _undeclared_checkout(self, source: Path):
        state = preserve_owned_mutable_state()
        state.__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        checkout_coordination._declared.clear()
        source_patch = patch.object(checkout_coordination, "_PACKAGE_SOURCE", source)
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def test_linked_checkout_is_derived_from_loaded_source_not_cwd(self) -> None:
        checkout, source = self._checkout()

        location = resolve_checkout_location(source)

        self.assertIsNotNone(location)
        assert location is not None
        self.assertEqual(location.kind, "linked")
        self.assertEqual(location.checkout_root, checkout)
        self.assertEqual(
            location.coordination_root,
            checkout.parent / "provider-runtime" / "dev-ar-coordination",
        )

    def test_checkout_resolution_skips_incomplete_nested_repository_shapes(self) -> None:
        checkout, _source = self._checkout()
        missing_package = checkout / "missing-package"
        (missing_package / "mcp").mkdir(parents=True)
        (missing_package / "mcp" / "pyproject.toml").touch()
        missing_marker = missing_package / "missing-marker"
        source = missing_marker / "mcp" / "src" / "agents_remember" / "candidate.py"
        source.parent.mkdir(parents=True)
        source.touch()
        (missing_marker / "mcp" / "pyproject.toml").touch()

        location = resolve_checkout_location(source)

        self.assertIsNotNone(location)
        assert location is not None
        self.assertEqual(location.checkout_root, checkout)

    def test_repository_shaped_directory_without_git_marker_is_not_a_checkout(self) -> None:
        source = self.root / "candidate" / "mcp" / "src" / "agents_remember" / "module.py"
        source.parent.mkdir(parents=True)
        source.touch()
        (self.root / "candidate" / "mcp" / "pyproject.toml").touch()

        self.assertIsNone(resolve_checkout_location(source))

    def test_undeclared_installed_package_path_has_no_checkout_override(self) -> None:
        state = preserve_owned_mutable_state()
        state.__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        checkout_coordination._declared.clear()
        installed_source = self.root / "site-packages" / "agents_remember" / "module.py"
        source_patch = patch.object(
            checkout_coordination,
            "_PACKAGE_SOURCE",
            installed_source,
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)

        self.assertIsNone(checkout_coordination.checkout_cli_location())

    def test_checkout_config_ignores_live_authority_and_uses_only_dummy_root(self) -> None:
        checkout, source = self._checkout()
        self._undeclared_checkout(source)
        live_settings = self.root / "live-settings.json"
        live_settings.write_text("not valid JSON", encoding="utf-8")

        config = load_config(live_settings)

        expected_root = checkout.parent / "provider-runtime" / "dev-ar-coordination"
        self.assertEqual(config.coordination_root, expected_root)
        self.assertEqual(config.repositories["agents-remember"].path, checkout)
        self.assertEqual(
            config.repositories["agents-remember"].memory_root,
            expected_root / "memory-repos" / "ar-agents-remember",
        )
        self.assertEqual(config.providers, {})
        self.assertFalse(config.dashboard.auto_start)
        self.assertFalse(config.benchmarks_enabled)
        self.assertFalse(config.retirement.auto_close_completed_seats)
        self.assertFalse(live_settings.parent.joinpath("live-ar-coordination").exists())

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_incident_shaped_inbox_write_lands_only_in_leaf_dummy_root(self) -> None:
        _checkout, source = self._checkout()
        self._undeclared_checkout(source)
        settings = self.root / "live-settings.json"
        self._settings(settings)
        config = load_config(settings)
        inbox = config.coordination_root / "observer" / "workspace" / "operator-inbox.jsonl"

        with exclusive_access(inbox, OPERATOR_INBOX_OWNERSHIP):
            append_line(inbox, '{"candidateField":"unpublished"}')

        self.assertTrue(inbox.is_file())
        self.assertEqual(
            inbox.read_text(encoding="utf-8"),
            '{"candidateField":"unpublished"}\n',
        )
        self.assertFalse((self.root / "live-ar-coordination").exists())

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_store_guard_refuses_escape_before_creating_lock_or_parent(self) -> None:
        _checkout, source = self._checkout()
        self._undeclared_checkout(source)
        escaped = self.root / "live-ar-coordination" / "operator-inbox.jsonl"

        guard = exclusive_access(escaped, OPERATOR_INBOX_OWNERSHIP)
        with self.assertRaisesRegex(CheckoutCoordinationError, "leaf-local"):
            guard.__enter__()

        self.assertFalse(escaped.parent.exists())
        self.assertFalse(escaped.with_name(f"{escaped.name}.lock").exists())

    def test_enclosure_report_write_is_allowed_without_opening_coordination_escape(self) -> None:
        checkout, source = self._checkout()
        self._undeclared_checkout(source)
        report = checkout.parent / "reports" / "closeout-operation.json"

        with exclusive_access(report, OPERATOR_INBOX_OWNERSHIP):
            rewrite_lines(report, ['{"status":"running"}'], OPERATOR_INBOX_OWNERSHIP)

        self.assertEqual(report.read_text(encoding="utf-8"), '{"status":"running"}\n')
        self.assertFalse((checkout.parent / "operator-inbox.jsonl").exists())

    def test_rewrite_guard_refuses_a_manually_constructed_live_target(self) -> None:
        _checkout, source = self._checkout()
        self._undeclared_checkout(source)
        escaped = self.root / "live-ar-coordination" / "operator-inbox.jsonl"

        with self.assertRaisesRegex(CheckoutCoordinationError, "refused target"):
            rewrite_lines(escaped, ["unsafe"], OPERATOR_INBOX_OWNERSHIP)

        self.assertFalse(escaped.parent.exists())

    def test_primary_checkout_undeclared_config_access_fails_closed(self) -> None:
        _checkout, source = self._checkout(linked=False)
        self._undeclared_checkout(source)
        settings = self.root / "live-settings.json"
        self._settings(settings)

        with self.assertRaisesRegex(ConfigError, "primary checkout is refused"):
            load_config(settings)

    def test_trusted_config_rejects_invalid_json(self) -> None:
        settings = self.root / "invalid-settings.json"
        settings.write_text("not valid JSON", encoding="utf-8")

        with preserve_owned_mutable_state():
            declare_process_role("mcp")
            with self.assertRaisesRegex(ConfigError, "cannot parse MCP settings JSON"):
                load_config(settings)

    def test_trusted_config_rejects_non_object_json(self) -> None:
        settings = self.root / "invalid-settings.json"
        settings.write_text("[]", encoding="utf-8")

        with preserve_owned_mutable_state():
            declare_process_role("mcp")
            with self.assertRaisesRegex(ConfigError, "must be a JSON object"):
                load_config(settings)

    def test_trusted_mcp_preserves_regular_authority_config(self) -> None:
        _checkout, source = self._checkout()
        self._undeclared_checkout(source)
        settings = self.root / "live-settings.json"
        self._settings(settings)
        declare_process_role("mcp")

        config = load_config(settings)

        self.assertEqual(config.coordination_root, self.root / "live-ar-coordination")
        self.assertTrue(config.retirement.auto_close_completed_seats)

    def test_explicit_test_mode_preserves_temporary_store_writes(self) -> None:
        _checkout, source = self._checkout()
        self._undeclared_checkout(source)
        target = self.root / "pytest-temp-root" / "records.jsonl"
        declare_test_process()

        append_line(target, "test-row")

        self.assertEqual(target.read_text(encoding="utf-8"), "test-row\n")

    def test_lifecycle_operation_uses_live_authority_without_claiming_daemon_role(self) -> None:
        _checkout, source = self._checkout()
        self._undeclared_checkout(source)
        settings = self.root / "live-settings.json"
        self._settings(settings)

        declare_lifecycle_operation_process()
        config = load_config(settings)

        self.assertEqual(declared_execution_mode(), "lifecycle-operation")
        self.assertIsNone(declared_daemon_role())
        self.assertEqual(declared_process_role(), "lifecycle-operation")
        self.assertEqual(config.coordination_root, self.root / "live-ar-coordination")
