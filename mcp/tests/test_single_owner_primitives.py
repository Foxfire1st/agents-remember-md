"""Small input/output checks for the supported single-writer structural detectors."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

import pytest
from agents_remember_test_support.code_quality import single_owner

PACKAGE_ROOT = MCP_SRC / "agents_remember"


def _git(source: str) -> list[str]:
    """What the git rule reports for ``source``, as ``line [form]`` strings."""
    return [
        f"{offender.line} [{offender.form}]"
        for offender in single_owner.module_git_offenders(ast.parse(source), "fixture.py")
    ]


def _replace(source: str) -> list[str]:
    """What the atomic-write rule reports for ``source``."""
    return [
        f"{offender.line} [{offender.form}]"
        for offender in single_owner.module_replace_offenders(ast.parse(source), "fixture.py")
    ]


def _task_writers(source: str, module: str = "fixture.py") -> list[str]:
    """What the task-document writer census reports for one module."""
    return [
        f"{site.line} [{site.form}] {site.detail}"
        for site in single_owner.module_task_document_writer_sites(ast.parse(source), module)
    ]


@pytest.mark.fitness
class GitSweepReachTests(unittest.TestCase):
    """Every bypass the git rule claims to catch, planted and required to be caught."""

    def test_an_import_alias_is_followed_to_the_name_it_binds(self) -> None:
        source = "from subprocess import run as spawn\nspawn(['git', 'status'])\n"
        self.assertEqual(_git(source), ["2 [git spawn]"])

    def test_a_program_name_hidden_behind_a_constant_is_resolved(self) -> None:
        plain = "import subprocess\nBINARY = 'git'\nsubprocess.run([BINARY, 'status'])\n"
        annotated = "import subprocess\nBINARY: str = 'git'\nsubprocess.run([BINARY, 'status'])\n"
        chained = "import subprocess\nA = B = 'git'\nsubprocess.run([B, 'status'])\n"
        self.assertEqual(_git(plain), ["3 [git spawn]"])
        self.assertEqual(_git(annotated), ["3 [git spawn]"])
        self.assertEqual(_git(chained), ["3 [git spawn]"])

    def test_a_shell_command_string_is_read_down_to_its_program_word(self) -> None:
        source = "import subprocess\nsubprocess.run('git status', shell=True)\n"
        self.assertEqual(_git(source), ["2 [git spawn]"])


@pytest.mark.fitness
class GitSweepFalsePositiveTests(unittest.TestCase):
    """Known-good constructs the package really contains. None of these may be reported."""

    def test_gh_is_not_git(self) -> None:
        # worktrees/modules/landing.py: gh resolves the repository through git, so it takes
        # the same scrubbed environment -- and it is still not a git spawn.
        source = (
            "import subprocess\n"
            "subprocess.run(\n"
            "    ['gh', 'pr', 'list', '--head', head, '--json', 'number'],\n"
            "    cwd=repo,\n"
            "    env=git_environment(),\n"
            ")\n"
        )
        self.assertEqual(_git(source), [])


@pytest.mark.fitness
class ReplaceSweepReachTests(unittest.TestCase):
    """Every way to reach the replace syscall, planted and required to be caught."""

    def test_the_module_attribute_form_is_caught(self) -> None:
        self.assertEqual(_replace("import os\nos.replace(tmp, path)\n"), ["2 [os.replace]"])


@pytest.mark.fitness
class ReplaceSweepFalsePositiveTests(unittest.TestCase):
    """The 83 near neighbours measured in the package. None of these may be reported."""

    def test_a_bare_replace_from_dataclasses_is_not_the_one_from_os(self) -> None:
        # observer/landing_state.py and serving/terminal_catalog.py both do exactly this.
        source = "from dataclasses import dataclass, replace\nrow = replace(entry, at=now)\n"
        self.assertEqual(_replace(source), [])
        self.assertEqual(_replace("from dataclasses import replace\nrow = replace(entry)\n"), [])


class TaskDocumentWriterCensusTests(unittest.TestCase):
    """The production-writer census follows the import forms a new caller can use."""

    def test_direct_imports_and_aliases_are_caught(self) -> None:
        direct = "from agents_remember.tasks import write_task_docs\nwrite_task_docs(root, docs)\n"
        alias = (
            "from agents_remember.tasks.store import write_task_doc as publish\n"
            "publish(root, doc)\n"
        )
        self.assertEqual(
            _task_writers(direct), ["2 [TaskDocument writer] calls write_task_docs(...)"]
        )
        self.assertEqual(
            _task_writers(alias), ["2 [TaskDocument writer] calls write_task_doc(...)"]
        )
        batch = (
            "from agents_remember.tasks import write_task_doc_batch as publish\n"
            "publish(documents)\n"
        )
        self.assertEqual(
            _task_writers(batch),
            ["2 [TaskDocument writer] calls write_task_doc_batch(...)"],
        )

    def test_reexport_without_a_call_and_unrelated_local_names_are_not_callers(self) -> None:
        reexport = "from agents_remember.tasks import write_task_docs\n"
        local = "def write_task_docs(root, docs):\n    return docs\nwrite_task_docs(root, docs)\n"
        self.assertEqual(_task_writers(reexport), [])
        self.assertEqual(_task_writers(local), [])


if __name__ == "__main__":
    unittest.main()
