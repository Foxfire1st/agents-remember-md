"""Report changed-production coverage at combined unit/integration delivery.

The gate compares Coverage.py's statements and branch arcs with lines added since the
resolved merge base. Every uncovered changed line and untaken changed branch is named.
The delivery report combines unit and integration coverage for diagnosis only.

Reported non-measured states are explicit: ``no-changed-lines``,
``no-python-changes``, and ``no-measurable-changes``. The last state includes affected
paths and line counts. A repository without a merge base uses git's empty tree, making
every tracked line part of the diff instead of vacuously passing.

Known boundary: coverage data cannot score Python outside the measured packages. Those
files remain visible in ``unmeasured_files`` and require measurement configuration or a
separate gate.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agents_remember.kernel import git_command

from agents_remember_test_support.code_quality import crap_calculator

# Combined delivery coverage of changed statements and branches.

# git's empty tree. Diffing against it yields the whole tree, which is what "no merge
# base" honestly means: nothing has been established yet, so nothing is grandfathered.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


class DiffScopeError(RuntimeError):
    """The gate could not work out which lines this change touched."""


@dataclass(frozen=True)
class BaseResolution:
    """Which revision the diff is taken against, and how that was decided.

    ``origin`` is printed on every run. A gate that silently picks its own comparison
    point can be made to certify nothing by picking the wrong one, so the choice is
    always visible in the output next to the verdict it produced.
    """

    revision: str
    origin: str


@dataclass(frozen=True)
class DiffCoverage:
    """One run's verdict."""

    base: BaseResolution
    state: str
    covered_units: int
    total_units: int
    uncovered_lines: tuple[str, ...]
    untaken_branches: tuple[str, ...]
    unmeasured_files: tuple[tuple[str, int], ...]
    changed_files: int

    @property
    def ratio(self) -> float:
        if not self.total_units:
            return 1.0
        return self.covered_units / self.total_units

    @property
    def percent(self) -> float:
        return self.ratio * 100.0


def _git(project_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run git with literal paths and convert execution errors to gate errors.

    ``core.quotePath=false`` keeps non-ASCII paths parseable. The shared runner strips
    hook-exported repository variables so a pre-push invocation measures ``project_root``.
    """

    try:
        return git_command.run_git(project_root, ["-c", "core.quotePath=false", *arguments])
    except (OSError, subprocess.SubprocessError) as error:
        raise DiffScopeError(f"git command failed (git {' '.join(arguments)}): {error}") from error


def run_git(project_root: Path, arguments: list[str]) -> str:
    completed = _git(project_root, arguments)
    if completed.returncode != 0:
        raise DiffScopeError(
            f"git command failed (git {' '.join(arguments)}): "
            f"exit {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def revision_exists(project_root: Path, revision: str) -> bool:
    completed = _git(project_root, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
    return completed.returncode == 0


def merge_base(project_root: Path, revision: str) -> str | None:
    completed = _git(project_root, ["merge-base", "HEAD", revision])
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def candidate_sources(environment: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(revision, origin)`` pairs to try, in order, most explicit first.

    The order is deliberate and each entry earns its place:

    ``AR_GATE_DIFF_BASE``
        The one way to say it outright. A leaf worktree branched from a series branch
        is the case git cannot infer -- ``main`` is not its source and it has no
        upstream -- so the hooks and the closeout path pass this.
    ``GITHUB_BASE_REF``
        A pull request states its own base. This is what CI uses.
    ``@{upstream}``
        Git's own record of where a branch came from, when one is configured.
    ``origin/HEAD`` then ``main``
        The default branch, which is the source branch for anything cut from it.
    """
    explicit = environment.get("AR_GATE_DIFF_BASE", "").strip()
    candidates: list[tuple[str, str]] = []
    if explicit:
        candidates.append((explicit, "AR_GATE_DIFF_BASE"))
    base_ref = environment.get("GITHUB_BASE_REF", "").strip()
    if base_ref:
        candidates.append((f"origin/{base_ref}", "GITHUB_BASE_REF (pull request base)"))
        candidates.append((base_ref, "GITHUB_BASE_REF (pull request base)"))
    candidates.append(("@{upstream}", "the branch's configured upstream"))
    candidates.append(("origin/HEAD", "the remote's default branch"))
    candidates.append(("main", "the local default branch"))
    return candidates


def resolve_base(
    project_root: Path,
    *,
    explicit_base: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> BaseResolution:
    """The revision to diff against.

    ``explicit_base`` is the ``--diff-base`` flag and wins over everything, including
    the environment. When nothing resolves -- an orphan branch, a first commit, a clone
    with no remote and no ``main`` -- the base is the empty tree rather than a skip.
    """
    env = os.environ if environment is None else environment
    if explicit_base:
        if not revision_exists(project_root, explicit_base):
            raise DiffScopeError(
                f"--diff-base {explicit_base!r} is not a commit in this repository"
            )
        return BaseResolution(revision=explicit_base, origin="--diff-base")
    for revision, origin in candidate_sources(env):
        if not revision_exists(project_root, revision):
            continue
        base = merge_base(project_root, revision)
        if base is not None:
            return BaseResolution(revision=base, origin=f"merge base with {revision} ({origin})")
    return BaseResolution(
        revision=EMPTY_TREE,
        origin="no merge base is resolvable; comparing against the empty tree",
    )


def changed_python_lines(project_root: Path, base: str) -> dict[str, set[int]]:
    """Line numbers this change added or modified, keyed by repo-relative path.

    The comparison is base-to-working-tree, with no second revision, because the
    working tree is what the suite just imported and measured. The numbers therefore
    index the same file content the coverage report describes.
    """
    return parse_unified_diff(
        run_git(
            project_root,
            [
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                "--diff-filter=ACMR",
                base,
                "--",
                "*.py",
            ],
        )
    )


def parse_unified_diff(diff: str) -> dict[str, set[int]]:
    """Post-image line numbers per file, from ``diff --unified=0`` output.

    A separate function from the git call because its two guards are only reachable
    from malformed input: a ``+++`` header that is not ``+++ b/<path>`` (which
    ``--diff-filter=ACMR`` never produces, since a deletion is filtered out before it
    can write ``+++ /dev/null``), and a ``@@`` line the hunk pattern does not match.
    Both drop the content rather than guess at it, and both are exercised directly by
    ``mcp/tests/test_diff_coverage.py`` -- a guard no test can reach is a guard nobody
    can show is right.
    """
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            if current is not None:
                changed.setdefault(current, set())
            continue
        if current is None or not line.startswith("@@"):
            continue
        match = HUNK.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        changed[current].update(range(start, start + count))
    return {path: lines for path, lines in changed.items() if lines}


def coverage_by_relative_path(
    coverage_json: Path, project_root: Path
) -> dict[str, crap_calculator.FileCoverage]:
    """The coverage report re-keyed on repo-relative posix paths.

    ``crap_calculator`` already normalises every spelling Coverage.py might emit; this
    keeps the single repo-relative key, which is the one ``git diff`` produces.
    """
    by_key = crap_calculator.load_coverage_by_path(coverage_json, project_root)
    relative: dict[str, crap_calculator.FileCoverage] = {}
    for key, coverage in by_key.items():
        candidate = Path(key)
        if candidate.is_absolute():
            continue
        relative.setdefault(candidate.as_posix().casefold(), coverage)
    return relative


@dataclass
class UnitTally:
    """Running totals while walking the diff, and the findings behind them."""

    covered: int = 0
    total: int = 0
    uncovered_lines: list[str] = field(default_factory=list)
    untaken_branches: list[str] = field(default_factory=list)


def tally_file(
    tally: UnitTally,
    path: str,
    lines: set[int],
    coverage: crap_calculator.FileCoverage,
) -> None:
    """Fold one changed file's measurable units into ``tally``.

    A unit is a measurable statement or a branch arc leaving one of the changed lines,
    which is Coverage.py's own accounting and the same one ``crap_calculator`` uses. A
    changed line that carries no statement -- a comment, a blank, a continuation --
    contributes nothing rather than counting as covered.
    """
    executed = coverage.executed_lines & lines
    missing = coverage.missing_lines & lines
    taken = {arc for arc in coverage.executed_branches if arc[0] in lines}
    untaken = {arc for arc in coverage.missing_branches if arc[0] in lines}
    tally.covered += len(executed) + len(taken)
    tally.total += len(executed) + len(missing) + len(taken) + len(untaken)
    tally.uncovered_lines.extend(f"{path}:{number}" for number in sorted(missing))
    tally.untaken_branches.extend(
        f"{path}:{source} -> {describe_destination(destination)}"
        for source, destination in sorted(untaken)
    )


def describe_destination(destination: int) -> str:
    """Coverage.py writes a function exit as a negative destination line."""
    return "exit" if destination < 0 else str(destination)


def measure(
    project_root: Path,
    coverage_json: Path,
    base: BaseResolution,
) -> DiffCoverage:
    """Score the changed lines, or report why there is nothing to score."""
    changed = changed_python_lines(project_root, base.revision)
    if not changed:
        return empty_result(base, state_for_empty_diff(project_root, base.revision))
    coverage = coverage_by_relative_path(coverage_json, project_root)
    tally = UnitTally()
    unmeasured: list[tuple[str, int]] = []
    for path in sorted(changed):
        file_coverage = coverage.get(path.casefold())
        if file_coverage is None:
            unmeasured.append((path, len(changed[path])))
            continue
        tally_file(tally, path, changed[path], file_coverage)
    state = "measured" if tally.total else "no-measurable-changes"
    return DiffCoverage(
        base=base,
        state=state,
        covered_units=tally.covered,
        total_units=tally.total,
        uncovered_lines=tuple(tally.uncovered_lines),
        untaken_branches=tuple(tally.untaken_branches),
        unmeasured_files=tuple(unmeasured),
        changed_files=len(changed),
    )


def state_for_empty_diff(project_root: Path, base: str) -> str:
    """Tell "nothing changed" apart from "nothing *Python* changed".

    Both pass, and they are different facts about the change: one says the branch is
    empty, the other says it edited documentation or a workflow. Collapsing them into a
    single "skipped" is how a gate stops being readable.
    """
    diff = run_git(project_root, ["diff", "--name-only", "--diff-filter=ACMR", base])
    return "no-python-changes" if diff.strip() else "no-changed-lines"


def empty_result(base: BaseResolution, state: str) -> DiffCoverage:
    return DiffCoverage(
        base=base,
        state=state,
        covered_units=0,
        total_units=0,
        uncovered_lines=(),
        untaken_branches=(),
        unmeasured_files=(),
        changed_files=0,
    )


def render(result: DiffCoverage) -> list[str]:
    """The report, findings and all.

    Every uncovered line is named. The percentage alone trains people to add any test
    until the number moves; the list says which line has never run, which is the only
    form of the finding someone can act on directly.
    """
    lines = [
        f"base: {result.base.revision} -- {result.base.origin}",
        "policy: diagnostic only; no coverage floor",
        f"state: {result.state}",
    ]
    lines.extend(STATE_NOTES.get(result.state, ()))
    if result.unmeasured_files:
        lines.append(unmeasured_header(result))
        lines.extend(
            f"  {count:>5} changed line(s)  {path}" for path, count in result.unmeasured_files
        )
    if result.state != "measured":
        return lines
    lines.append(
        f"changed coverage: {result.covered_units}/{result.total_units} units "
        f"= {result.percent:.2f}%"
    )
    lines.append(
        f"\n{len(result.uncovered_lines)} changed line(s) and "
        f"{len(result.untaken_branches)} changed branch(es) were not observed. "
        "Use these observations to assess meaningful behavior; they do not require new tests."
    )
    lines.extend(f"  uncovered line    {entry}" for entry in result.uncovered_lines)
    lines.extend(f"  untaken branch    {entry}" for entry in result.untaken_branches)
    return lines


STATE_NOTES: dict[str, tuple[str, ...]] = {
    "no-changed-lines": (
        "nothing changed against the base, so there is nothing for a coverage report to measure.",
    ),
    "no-python-changes": (
        "files changed against the base, none of them Python; the report has nothing to measure.",
    ),
    "no-measurable-changes": (
        "Python changed, but no changed line sits inside a package the coverage run measures.",
    ),
}


def unmeasured_header(result: DiffCoverage) -> str:
    """Name the changed Python the coverage run does not measure.

    ``--cov`` is aimed at the tracked top-level packages, which is the shipped code; the
    suite itself, ``scripts/`` and the provider images are outside it. Their changed
    lines cannot be scored, so they are listed on every run instead of being dropped --
    an unscored file that nobody prints is indistinguishable from a covered one.
    """
    total = sum(count for _path, count in result.unmeasured_files)
    return (
        f"\n{len(result.unmeasured_files)} changed Python file(s) carrying {total} changed "
        "line(s) are outside the packages coverage measures, so this report cannot speak "
        "for them:"
    )
