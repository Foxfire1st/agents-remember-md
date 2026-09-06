"""Tests for the CGC watcher entrypoint guard Docker asset.

The guard runs inside the runner image where `redis` is installed as a cgc
dependency; on the host a stub module is injected before import so the asset
can be loaded and its self-heal logic tested directly.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

GUARD_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agents_remember"
    / "package_data"
    / "runtime"
    / "providers"
    / "docker"
    / "codegraphcontext"
    / "watch_guard.py"
)


class FakeRedisError(Exception):
    pass


class FakeBusyLoadingError(FakeRedisError):
    pass


class FakeResponseError(FakeRedisError):
    pass


def _fake_redis_module() -> mock.MagicMock:
    module = mock.MagicMock(name="redis")
    module.exceptions.RedisError = FakeRedisError
    module.exceptions.BusyLoadingError = FakeBusyLoadingError
    module.exceptions.ResponseError = FakeResponseError
    return module


def load_guard() -> types.ModuleType:
    fake_redis = _fake_redis_module()
    with mock.patch.dict(
        sys.modules, {"redis": fake_redis, "redis.exceptions": fake_redis.exceptions}
    ):
        spec = importlib.util.spec_from_file_location("cgc_watch_guard", GUARD_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class WatchGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.guard = load_guard()

    def test_wait_for_ready_gives_up_after_deadline(self) -> None:
        with mock.patch.object(self.guard, "_connection") as connection:
            connection.return_value.ping.side_effect = FakeRedisError("nope")
            self.assertIsNone(self.guard.wait_for_ready(0))

    def test_clear_poisoned_graph_keeps_indexed_graph(self) -> None:
        client = mock.Mock()
        client.execute_command.return_value = [[b"count(f)"], [[42]], [b"stats"]]
        self.guard.clear_poisoned_graph(client, "cgc_repo", 1)
        delete_calls = [
            call for call in client.execute_command.call_args_list if call.args[0] == "GRAPH.DELETE"
        ]
        self.assertEqual(delete_calls, [])

    def test_main_checks_graph_then_execs_cgc(self) -> None:
        client = mock.Mock()
        client.execute_command.return_value = [[b"count(f)"], [[7]], [b"stats"]]
        env = {"FALKORDB_GRAPH_NAME": "cgc_repo"}
        with (
            mock.patch.object(self.guard.os, "environ", env),
            mock.patch.object(self.guard, "wait_for_ready", return_value=client),
            mock.patch.object(self.guard.os, "execvp") as execvp,
            mock.patch.object(self.guard.sys, "argv", ["cgc-watch-guard.py", "watch", "/repo"]),
        ):
            self.guard.main()
        execvp.assert_called_once_with("cgc", ["cgc", "watch", "/repo"])


if __name__ == "__main__":
    unittest.main()
