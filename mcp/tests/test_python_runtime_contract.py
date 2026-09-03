from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _contract() -> dict[str, str]:
    values: dict[str, str] = {}
    path = REPOSITORY_ROOT / "scripts/python-runtime-contract.env"
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip("'\"")
    return values


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


@dataclass(frozen=True)
class _RuntimeFixture:
    contract: dict[str, str]
    environment: dict[str, str]
    cache_root: Path
    tooling_root: Path
    log: Path


def _runtime_fixture(tmp_path: Path) -> _RuntimeFixture:
    contract = _contract()
    cache_root = tmp_path / "cache"
    tooling_root = tmp_path / "tooling"
    shim_root = tmp_path / "bin"
    log = tmp_path / "git.log"
    (cache_root / "downloads").mkdir(parents=True)
    tooling_root.mkdir()
    shim_root.mkdir()
    archive = cache_root / "downloads" / f"Python-{contract['AR_PYTHON_VERSION']}.tar.xz"
    archive.write_text("verified fixture source\n", encoding="utf-8")
    _write_executable(
        shim_root / "sha256sum",
        '#!/usr/bin/env sh\nprintf \'%s  %s\\n\' "$AR_TEST_SOURCE_SHA" "$1"\n',
    )
    _write_executable(shim_root / "git", _GIT_SHIM)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{shim_root}:{environment['PATH']}",
            "AR_TEST_LOG": log.as_posix(),
            "AR_TEST_BUILD_COMMIT": contract["AR_PYTHON_BUILD_COMMIT"],
            "AR_TEST_PYTHON_VERSION": contract["AR_PYTHON_VERSION"],
            "AR_TEST_SOURCE_SHA": contract["AR_PYTHON_SOURCE_SHA256"],
            "AR_TEST_SOURCE_URL": contract["AR_PYTHON_SOURCE_URL"],
        }
    )
    return _RuntimeFixture(contract, environment, cache_root, tooling_root, log)


def _run_installer(
    tmp_path: Path,
    fixture: _RuntimeFixture,
    runtime_name: str,
) -> subprocess.CompletedProcess[str]:
    prefix = tmp_path / runtime_name / f"cpython-{fixture.contract['AR_PYTHON_VERSION']}"
    return subprocess.run(
        [
            "bash",
            (REPOSITORY_ROOT / "scripts/install-python-runtime.sh").as_posix(),
            "--prefix",
            prefix.as_posix(),
            "--cache-root",
            fixture.cache_root.as_posix(),
            "--tooling-root",
            fixture.tooling_root.as_posix(),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=fixture.environment,
    )


_GIT_SHIM = r"""#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$AR_TEST_LOG"
if [ "$1" = clone ]; then
  destination="$4"
  mkdir -p "$destination/.git"
  if [ "${AR_TEST_CLONE_FAIL:-0}" = 1 ]; then
    printf 'partial\n' > "$destination/partial"
    exit 23
  fi
  if [ -n "${AR_TEST_CLONE_BARRIER:-}" ]; then
    mkdir -p "$AR_TEST_CLONE_BARRIER"
    printf 'ready\n' > "$AR_TEST_CLONE_BARRIER/clone.$$"
    attempts=0
    while :; do
      set -- "$AR_TEST_CLONE_BARRIER"/clone.*
      [ "$#" -ge 2 ] && break
      attempts=$((attempts + 1))
      [ "$attempts" -lt 500 ] || exit 92
      sleep 0.01
    done
  fi
  case "${AR_TEST_RACE_FOREIGN:-}" in
    directory)
      mkdir -p "$AR_TEST_RACE_TARGET"
      printf 'foreign\n' > "$AR_TEST_RACE_TARGET/foreign-content"
      ;;
    symlink)
      mkdir -p "$AR_TEST_RACE_SYMLINK_TARGET"
      printf 'foreign\n' > "$AR_TEST_RACE_SYMLINK_TARGET/foreign-content"
      ln -s "$AR_TEST_RACE_SYMLINK_TARGET" "$AR_TEST_RACE_TARGET"
      ;;
  esac
  exit 0
fi
root="$2"
case "$3" in
  checkout)
    definition="$root/plugins/python-build/share/python-build/$AR_TEST_PYTHON_VERSION"
    executable="$root/plugins/python-build/bin/python-build"
    mkdir -p "$(dirname -- "$definition")" "$(dirname -- "$executable")"
    printf '%s#%s\n' "$AR_TEST_SOURCE_URL" "$AR_TEST_SOURCE_SHA" > "$definition"
    cat > "$executable" <<'PYTHON_BUILD'
#!/usr/bin/env sh
set -eu
printf 'python-build %s\n' "$*" >> "$AR_TEST_LOG"
prefix="$3"
mkdir -p "$prefix/bin"
printf '#!/usr/bin/env sh\nexit 0\n' > "$prefix/bin/python3.13"
chmod +x "$prefix/bin/python3.13"
PYTHON_BUILD
    chmod +x "$executable"
    ;;
  rev-parse)
    printf '%s\n' "$AR_TEST_BUILD_COMMIT"
    ;;
  *)
    exit 91
    ;;
esac
"""


def test_runtime_contract_has_one_exact_supported_minor() -> None:
    contract = _contract()
    package = (REPOSITORY_ROOT / "mcp/pyproject.toml").read_text(encoding="utf-8")
    quality = (REPOSITORY_ROOT / ".github/workflows/quality-checks.yml").read_text(encoding="utf-8")
    release = (REPOSITORY_ROOT / ".github/workflows/publish-mcp-to-pypi.yml").read_text(
        encoding="utf-8"
    )
    dagger_python = (REPOSITORY_ROOT / ".dagger/.python-version").read_text(encoding="utf-8")

    assert contract["AR_PYTHON_VERSION"] == "3.13.15"
    assert contract["AR_PYTHON_REQUIRES"] == ">=3.13,<3.14"
    assert 'requires-python = ">=3.13,<3.14"' in package
    assert '"Programming Language :: Python :: 3.13"' in package
    assert "Programming Language :: Python :: 3.11" not in package
    assert "Programming Language :: Python :: 3.12" not in package
    assert 'python-version: "3.13.15"' in quality
    assert 'python-version: "3.13.15"' in release
    assert dagger_python.strip() == contract["AR_PYTHON_VERSION"]


@pytest.mark.skipif(sys.platform != "linux", reason="pidfd is the Linux executor contract")
def test_current_test_interpreter_passes_the_canonical_capability_probe() -> None:
    contract = _contract()
    completed = subprocess.run(
        [
            sys.executable,
            (REPOSITORY_ROOT / "scripts/check-python-runtime.py").as_posix(),
            "--expected-version",
            contract["AR_PYTHON_VERSION"],
            "--require-linux-pidfd",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert report["version"] == contract["AR_PYTHON_VERSION"]
    assert report["capabilities"] == {
        "os.pidfd_open": True,
        "signal.pidfd_send_signal": True,
    }


def test_runtime_builder_is_fully_cloned_atomically_published_and_reused(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    first = _run_installer(tmp_path, fixture, "runtime-one")
    assert first.returncode == 0, first.stderr

    builder = fixture.tooling_root / f"pyenv-{fixture.contract['AR_PYTHON_BUILD_COMMIT'][:12]}"
    clone_lines = [
        line
        for line in fixture.log.read_text(encoding="utf-8").splitlines()
        if line.startswith("clone ")
    ]
    installer = (REPOSITORY_ROOT / "scripts/install-python-runtime.sh").read_text(encoding="utf-8")
    assert builder.is_dir()
    assert not list(fixture.tooling_root.glob(".pyenv-*.staging.*"))
    assert len(clone_lines) == 1
    assert "clone --no-checkout" in clone_lines[0]
    assert "--filter" not in clone_lines[0]
    assert installer.count('mv -T --no-clobber -- "$builder_staging" "$builder_root"') == 1
    assert installer.index('validate_builder "$builder_staging"') < installer.index("mv -T --")

    sentinel = builder / "reuse-sentinel"
    sentinel.write_text("preserve\n", encoding="utf-8")
    second = _run_installer(tmp_path, fixture, "runtime-two")
    assert second.returncode == 0, second.stderr
    clone_lines = [
        line
        for line in fixture.log.read_text(encoding="utf-8").splitlines()
        if line.startswith("clone ")
    ]
    assert len(clone_lines) == 1
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_failed_builder_clone_leaves_no_canonical_or_staging_checkout(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    fixture.environment["AR_TEST_CLONE_FAIL"] = "1"

    failed = _run_installer(tmp_path, fixture, "failed-runtime")

    builder = fixture.tooling_root / f"pyenv-{fixture.contract['AR_PYTHON_BUILD_COMMIT'][:12]}"
    clone_lines = [
        line
        for line in fixture.log.read_text(encoding="utf-8").splitlines()
        if line.startswith("clone ")
    ]
    assert failed.returncode == 23
    assert not builder.exists()
    assert not list(fixture.tooling_root.glob(".pyenv-*.staging.*"))
    assert len(clone_lines) == 1


def test_existing_foreign_builder_is_refused_and_preserved(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    builder = fixture.tooling_root / f"pyenv-{fixture.contract['AR_PYTHON_BUILD_COMMIT'][:12]}"
    builder.mkdir()
    marker = builder / "foreign-content"
    marker.write_text("keep\n", encoding="utf-8")

    failed = _run_installer(tmp_path, fixture, "refused-runtime")

    assert failed.returncode == 1
    assert f"refusing foreign builder path: {builder}" in failed.stderr
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert not fixture.log.exists()
    assert not list(fixture.tooling_root.glob(".pyenv-*.staging.*"))


def test_competing_publishers_adopt_only_the_validated_winner(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    fixture.environment["AR_TEST_CLONE_BARRIER"] = (tmp_path / "clone-barrier").as_posix()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_run_installer, tmp_path, fixture, runtime_name)
            for runtime_name in ("race-one", "race-two")
        ]
        results = [future.result(timeout=20) for future in futures]

    builder = fixture.tooling_root / f"pyenv-{fixture.contract['AR_PYTHON_BUILD_COMMIT'][:12]}"
    clone_lines = [
        line
        for line in fixture.log.read_text(encoding="utf-8").splitlines()
        if line.startswith("clone ")
    ]
    assert [result.returncode for result in results] == [0, 0]
    assert len(clone_lines) == 2
    assert builder.is_dir() and (builder / ".git").is_dir()
    assert not list(fixture.tooling_root.glob(".pyenv-*.staging.*"))


@pytest.mark.parametrize("foreign_kind", ["directory", "symlink"])
def test_foreign_builder_publication_race_fails_closed(
    tmp_path: Path,
    foreign_kind: str,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    builder = fixture.tooling_root / f"pyenv-{fixture.contract['AR_PYTHON_BUILD_COMMIT'][:12]}"
    backing = tmp_path / "foreign-backing"
    fixture.environment.update(
        {
            "AR_TEST_RACE_FOREIGN": foreign_kind,
            "AR_TEST_RACE_TARGET": builder.as_posix(),
            "AR_TEST_RACE_SYMLINK_TARGET": backing.as_posix(),
        }
    )

    failed = _run_installer(tmp_path, fixture, f"foreign-race-{foreign_kind}")

    preserved_root = backing if foreign_kind == "symlink" else builder
    assert failed.returncode == 1
    assert f"refusing foreign builder path: {builder}" in failed.stderr
    assert (preserved_root / "foreign-content").read_text(encoding="utf-8") == "foreign\n"
    assert builder.is_symlink() is (foreign_kind == "symlink")
    assert not list(fixture.tooling_root.glob(".pyenv-*.staging.*"))
