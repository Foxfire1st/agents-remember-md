import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import (
    contextmanager,
    redirect_stdout,
)
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel import filesystem
from agents_remember.kernel.memory_ledger import (
    create_initial_ledger,
    load_ledger,
    parse_ledger_text,
    prepend_mapping,
    write_ledger,
)
from agents_remember.memory import baseline as adopt_baseline
from agents_remember.models.closeout.input import CloseoutCorrectedCall
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    SprintExecutionGraph,
    SprintExecutionNode,
    TaskDocument,
    read_task_doc,
    write_task_doc,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.store import json_path_for
from agents_remember.worktrees import git_worktree_manager as worktree_manager
from agents_remember.worktrees.closeout_input import (
    normalize_closeout_input,
    raw_closeout_messages,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.route_review import (
    build_route_review,
    document_ref,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    contract_publication_text,
    default_contract,
    default_series_contract,
    load_contract,
    write_contract,
)
from closeout_input_test_support import MutationEvidenceRecorder
from curator_coherence_test_support import (
    write_curator_evidence,
    write_curator_task_topology,
)

drift = adopt_baseline.drift


def _benchmark_git_subcommands(recorder: mock.Mock) -> list[str]:
    """The git subcommands ``prepare_repo`` asked for, in order.

    ``run_git_command(repo_root, args, dry_run, ...)`` carries the args without the program
    word, so the subcommand is ``args[0]`` -- an exact match rather than the substring
    search over a joined argv these tests used when the benchmark runner built its own
    ``["git", "-c", ...]`` line.
    """
    return [call.args[1][0] for call in recorder.call_args_list]


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_worktree_support.py:53).
def git(repo: Path, *args: str) -> str:  # pragma: no cover
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_worktree_support.py:66).
def init_repo(repo: Path, branch: str = "main") -> str:  # pragma: no cover
    repo.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "init", "-b", branch],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        git(repo, "init")
        git(repo, "checkout", "-b", branch)
    git(repo, "config", "user.email", "agents-remember-tests@example.invalid")
    git(repo, "config", "user.name", "Agents Remember Tests")
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Initial commit")
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", f"refs/remotes/origin/{branch}", commit)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    return commit


def seed_memory_ledger(memory_repo: Path, repo_name: str, code_commit: str) -> str:
    """Commit the canonical initial ledger for an external-memory test source."""

    memory_content = git(memory_repo, "rev-parse", "HEAD")
    write_ledger(
        memory_repo / "memory.md",
        create_initial_ledger(repo_name, code_commit, memory_content),
    )
    git(memory_repo, "add", "memory.md")
    git(memory_repo, "commit", "-m", "Add memory ledger")
    return git(memory_repo, "rev-parse", "HEAD")


def write_current_task_lineage(
    coordination_root: Path,
    *,
    repo_name: str,
    master_name: str,
    leaf_id: str,
) -> Path:
    """Create a current organizational sprint-super -> leaf lineage."""
    repo = coordination_root / "fixture-repositories" / repo_name
    base = init_repo(repo, "main")
    super_branch = "super"
    leaf_branch = f"ar/{leaf_id}"
    git(repo, "branch", super_branch, "main")
    git(repo, "branch", leaf_branch, super_branch)
    task_root = coordination_root / "tasks" / repo_name
    master_path = task_root / master_name / "task.json"
    master = read_task_doc(master_path)
    write_task_doc(
        master_path.parent,
        master.model_copy(update={"executionNature": "organizational"}),
    )
    master_ref = TaskDocumentRef(repository=repo_name, path=f"{master_name}/task.json")
    sprint_path = task_root / "sprint" / "task.json"
    if sprint_path.exists():
        sprint = read_task_doc(sprint_path)
        sprint = sprint.model_copy(
            update={
                "orchestrates": [master_name],
                "integrationBranch": super_branch,
                "executionGraph": SprintExecutionGraph(
                    nodes=[SprintExecutionNode(ref=master_ref)], edges=[]
                ),
            }
        )
    else:
        sprint = TaskDocument.model_validate(
            {
                "id": "SPRINT",
                "slug": "sprint",
                "title": "Sprint",
                "kind": "master",
                "repo": repo_name,
                "createdAt": "2026-07-07T10:00",
                "orchestrates": [master_name],
                "integrationBranch": super_branch,
                "executionGraph": {"nodes": [master_ref.model_dump()], "edges": []},
            }
        )
    write_task_doc(sprint_path.parent, sprint)
    task = ContractTask(
        name=master_name,
        repo_name=repo_name,
        coordination_root=coordination_root,
        workflow_kind="light-task",
        memory_mode="disabled",
    )
    leaf = default_contract(
        task,
        leaf=LeafIdentity(worktree_name=leaf_id, leaf_id=leaf_id),
        code=RepoBranchPlan(
            repo_path=repo,
            source_branch=super_branch,
            work_branch=leaf_branch,
            base_commit=base,
        ),
    )
    write_contract(leaf.contract_path, leaf)
    return repo


def write_file_onboarding(
    onboarding_root: Path, repo_name: str, source_path: str, commit_hash: str
) -> None:
    path = onboarding_root / f"{source_path}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {source_path}",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| repository | {repo_name} |",
                f"| path | `{source_path}` |",
                "| doc_type | `file-level-onboarding` |",
                f"| lastVerifiedCommitHash | `{commit_hash}` |",
                "| lastVerifiedCommitDate | 2026-05-09T00:00:00+00:00 |",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_claim_onboarding(
    onboarding_root: Path, repo_name: str, source_path: str, commit_hash: str
) -> Path:
    path = onboarding_root / f"{source_path}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {source_path}",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| repository | {repo_name} |",
                f"| path | `{source_path}` |",
                "| doc_type | `file-level-onboarding` |",
                f"| lastVerifiedCommitHash | `{commit_hash}` |",
                "| lastVerifiedCommitDate | 2026-05-09T00:00:00+00:00 |",
                "",
                "## Repo-Internal References",
                "",
                "| Finding | Anchor | Source |",
                "| --- | --- | --- |",
                f"| Stable behavior. | `stable` | {source_path}:1-2 |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_route_overview(
    onboarding_root: Path, repo_name: str, source_route: str, commit_hash: str
) -> Path:
    path = (
        onboarding_root / source_route / "overview.md"
        if source_route != "."
        else onboarding_root / "overview.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {source_route} Overview",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| repository | {repo_name} |",
                "| doc_type | `route-local-overview` |",
                f"| sourceRoute | `{source_route}` |",
                f"| lastVerifiedCommitHash | `{commit_hash}` |",
                "| lastVerifiedCommitDate | 2026-05-09T00:00:00+00:00 |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_entity_catalog(
    onboarding_root: Path,
    repo_name: str,
    rows: list[tuple[str, str, str, list[str]]],
    inventory_entities: list[str] | None = None,
    include_fingerprints: bool = True,
) -> Path:
    path = onboarding_root / "entities.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if inventory_entities is None:
        inventory_entities = [entity for entity, _algorithm, _fingerprint, _evidence_paths in rows]
    lines = [
        "# Entities",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| repository | {repo_name} |",
        "| doc_type | `repo-entity-catalog` |",
        "| lastUpdated | 2026-05-09T00:00:00+00:00 |",
        "| status | active |",
        "",
    ]
    if include_fingerprints:
        lines.extend(
            [
                "## Entity Fingerprints",
                "",
                "| Entity | Algorithm | Fingerprint | Evidence Paths |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entity, algorithm, fingerprint, evidence_paths in rows:
            evidence = "; ".join(f"`{source_path}`" for source_path in evidence_paths)
            lines.append(f"| {entity} | `{algorithm}` | `{fingerprint}` | {evidence} |")
        lines.append("")
    lines.extend(["## Entity Inventory", ""])
    for entity in inventory_entities:
        lines.extend([f"### {entity}", ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_worktree_support.py:212).
def read_onboarding_field(path: Path, field: str) -> str:  # pragma: no cover
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"| {field} |"):
            return line.split("|", 3)[2].strip().strip("`")
    raise AssertionError(f"{field} was not found in {path}")


def commit_file(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def commit_memory_ledger(
    memory_repo: Path, code_commit: str, memory_content_commit: str, message: str
) -> str:
    ledger_path = memory_repo / "memory.md"
    ledger = parse_ledger_text(ledger_path.read_text(encoding="utf-8"))
    write_ledger(ledger_path, prepend_mapping(ledger, code_commit, memory_content_commit))
    git(memory_repo, "add", "memory.md")
    git(memory_repo, "commit", "-m", message)
    return git(memory_repo, "rev-parse", "HEAD")


def initialized_memory_repo(
    memory_repo: Path, repo_name: str, _code_branch: str, memory_branch: str, code_commit: str
) -> str:
    memory_content = init_repo(memory_repo, memory_branch)
    write_ledger(
        memory_repo / "memory.md", create_initial_ledger(repo_name, code_commit, memory_content)
    )
    settings_path = memory_repo / "system" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "version": 2,
                "onboarding": {
                    "storage": {"mode": "memory-repo"},
                    "pathRules": {
                        "include": {"paths": ["*"], "fileTypes": [".md", ".py"]},
                        "exclude": {"paths": []},
                    },
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    git(memory_repo, "add", "memory.md", "system/settings.json")
    git(memory_repo, "commit", "-m", "Add memory ledger")
    return git(memory_repo, "rev-parse", "HEAD")


def open_external_contract_fixture(root: Path):
    code_repo = root / "repo-a"
    code_base = init_repo(code_repo, "main")
    memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
    memory_seed = init_repo(memory_repo, "main")
    write_ledger(memory_repo / "memory.md", create_initial_ledger("repo-a", code_base, memory_seed))
    git(memory_repo, "add", "memory.md")
    git(memory_repo, "commit", "-m", "Add memory ledger")
    memory_base = git(memory_repo, "rev-parse", "HEAD")
    git(code_repo, "branch", "super", code_base)
    git(memory_repo, "branch", "super", memory_base)
    contract = default_contract(
        ContractTask(
            name="Commit Approval Thing",
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="chat-task",
            memory_mode="external",
        ),
        leaf=LeafIdentity(worktree_name="commit-approval-thing"),
        code=RepoBranchPlan(
            repo_path=code_repo,
            source_branch="super",
            work_branch="ar/commit-approval-thing",
            base_commit=code_base,
        ),
        memory=RepoBranchPlan(
            repo_path=memory_repo,
            source_branch="super",
            work_branch="ar/commit-approval-thing",
            base_commit=memory_base,
        ),
    )
    parent = default_series_contract(
        ContractTask(
            name="Commit Approval Master",
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="chat-task",
            memory_mode="external",
        ),
        code=RepoBranchPlan(
            repo_path=code_repo,
            source_branch="main",
            work_branch="super",
            base_commit=code_base,
        ),
        memory=RepoBranchPlan(
            repo_path=memory_repo,
            source_branch="main",
            work_branch="super",
            base_commit=memory_base,
        ),
    )
    write_contract(parent.contract_path, parent)
    publish_new_lifecycle_operation_location(
        parent,
        contract_text=contract_publication_text(parent.contract_path, parent),
    )
    contract = replace(contract, parent_contract_path=parent.contract_path)
    assert contract.memory_worktree is not None
    git(
        code_repo,
        "worktree",
        "add",
        "-b",
        contract.code_work_branch,
        str(contract.code_worktree),
        "super",
    )
    git(
        memory_repo,
        "worktree",
        "add",
        "-b",
        contract.memory_work_branch,
        str(contract.memory_worktree),
        "super",
    )
    write_contract(contract.contract_path, contract)
    publish_new_lifecycle_operation_location(
        contract,
        contract_text=contract_publication_text(contract.contract_path, contract),
    )
    return contract


def dirty_open_external_contract_fixture(root: Path):
    contract = open_external_contract_fixture(root)
    (contract.code_worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    assert contract.memory_worktree is not None
    write_file_onboarding(
        contract.memory_worktree / "onboarding",
        contract.repo_name,
        "feature.txt",
        contract.code_base_commit,
    )
    write_passing_route_review(contract)
    return contract


def write_passing_route_review(contract: WorktreeContract) -> None:
    report = contract.task_root / "notes/reports/test-reviewer-verdict.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Test reviewer verdict\n\nPass.\n", encoding="utf-8")
    document = TaskDocument.model_validate(
        {
            "id": contract.leaf_id,
            "slug": contract.leaf_id,
            "title": contract.task_name,
            "kind": "subTask",
            "repo": contract.repo_name,
            "createdAt": "2026-08-13T00:00:00+00:00",
            "lifecycleId": contract.lifecycle_id or None,
            "enclosures": [
                {
                    "leafId": contract.leaf_id,
                    "enclosurePath": contract.contract_path.as_posix(),
                }
            ],
        }
    )
    document_path = json_path_for(contract.task_root, document)
    review = build_route_review(
        contract,
        ResolvedTaskDocument(
            ref=document_ref(contract, document_path),
            path=document_path,
            document=document,
        ),
        {
            "verdict": "pass",
            "verdictRef": "notes/reports/test-reviewer-verdict.md",
            "routes": [
                {
                    "route": "test-fixture",
                    "verdict": "pass",
                    "evidenceRef": "notes/reports/test-reviewer-verdict.md",
                }
            ],
        },
        now=datetime(2026, 8, 13, tzinfo=UTC),
    )
    write_task_doc(
        contract.task_root,
        document.model_copy(update={"routeReview": review}),
    )
    if contract.memory_mode == "external" and contract.kind == "leaf":
        caller_ref = write_curator_task_topology(contract)
        write_curator_evidence(contract, caller_ref=caller_ref)


def claimed_external_contract_fixture(root: Path):
    contract = open_external_contract_fixture(root)
    baseline = commit_file(
        contract.code_worktree,
        "feature.py",
        "def stable(value: int) -> int:\n    return value + 1\n",
        "Add claimed baseline",
    )
    assert contract.memory_worktree is not None
    sidecar = write_claim_onboarding(
        contract.memory_worktree / "onboarding",
        contract.repo_name,
        "feature.py",
        baseline,
    )
    return contract, baseline, sidecar


def _series_parent_fixture(
    root: Path,
    *,
    task_name: str,
    code_repo: Path,
    memory_repo: Path,
) -> tuple[WorktreeContract, Path, Path]:
    """Create the real super -> master edge used by leaf lifecycle fixtures."""
    code_base = git(code_repo, "rev-parse", "HEAD")
    memory_base = git(memory_repo, "rev-parse", "HEAD")
    slug = task_name.lower().replace(" ", "-")
    code_branch = f"ar/{slug}"
    memory_branch = f"ar/{slug}"
    code_worktree = root / f"{slug}-master-code"
    memory_worktree = root / f"{slug}-master-memory"
    git(code_repo, "branch", "super", code_base)
    git(memory_repo, "branch", "super", memory_base)
    git(code_repo, "worktree", "add", "-b", code_branch, str(code_worktree), "super")
    git(memory_repo, "worktree", "add", "-b", memory_branch, str(memory_worktree), "super")
    parent = default_series_contract(
        ContractTask(
            name=task_name,
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="chat-task",
            memory_mode="external",
        ),
        code=RepoBranchPlan(
            repo_path=code_repo,
            source_branch="super",
            work_branch=code_branch,
            base_commit=code_base,
        ),
        memory=RepoBranchPlan(
            repo_path=memory_repo,
            source_branch="super",
            work_branch=memory_branch,
            base_commit=memory_base,
        ),
    )
    parent = replace(
        parent,
        code_worktree=code_worktree,
        memory_worktree=memory_worktree,
        ledger_path=memory_worktree / "memory.md",
    )
    write_contract(parent.contract_path, parent)
    return parent, code_worktree, memory_worktree


def committed_range_external_contract_fixture(root: Path):
    """Open external contract whose changes are already committed on the work branch.

    The feature.txt sidecar exists at the memory baseline so the body gate can
    classify it, feature.txt and raw.txt arrive via work-branch commits
    (raw.txt has no onboarding), and the working tree is clean.
    """
    code_repo = root / "repo-a"
    code_base = init_repo(code_repo, "main")
    memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
    memory_seed = init_repo(memory_repo, "main")
    write_ledger(memory_repo / "memory.md", create_initial_ledger("repo-a", code_base, memory_seed))
    write_file_onboarding(memory_repo / "onboarding", "repo-a", "feature.txt", code_base)
    git(memory_repo, "add", "-A")
    git(memory_repo, "commit", "-m", "Add memory baseline")
    memory_base = git(memory_repo, "rev-parse", "HEAD")
    parent, code_master, memory_master = _series_parent_fixture(
        root,
        task_name="Committed Range Thing",
        code_repo=code_repo,
        memory_repo=memory_repo,
    )
    contract = default_contract(
        ContractTask(
            name="Committed Range Thing",
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="chat-task",
            memory_mode="external",
        ),
        leaf=LeafIdentity(worktree_name="committed-range-thing"),
        code=RepoBranchPlan(
            repo_path=code_master,
            source_branch=parent.code_work_branch,
            work_branch="ar/committed-range-thing-leaf",
            base_commit=code_base,
        ),
        memory=RepoBranchPlan(
            repo_path=memory_master,
            source_branch=parent.memory_work_branch,
            work_branch="ar/committed-range-thing-leaf",
            base_commit=memory_base,
        ),
    )
    assert contract.memory_worktree is not None
    git(
        code_master,
        "worktree",
        "add",
        "-b",
        contract.code_work_branch,
        str(contract.code_worktree),
        parent.code_work_branch,
    )
    git(
        memory_master,
        "worktree",
        "add",
        "-b",
        contract.memory_work_branch,
        str(contract.memory_worktree),
        parent.memory_work_branch,
    )
    commit_file(contract.code_worktree, "feature.txt", "feature v2\n", "Add feature")
    commit_file(contract.code_worktree, "raw.txt", "raw\n", "Add raw transport")
    contract = replace(contract, parent_contract_path=parent.contract_path)
    write_contract(contract.contract_path, contract)
    write_passing_route_review(contract)
    return contract


def long_source_path() -> str:
    segments = [f"segment-{index:02d}" for index in range(30)]
    return "/".join(["mcp", "src", *segments, "deep_file.py"])


@contextmanager
def long_path_tempdir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(filesystem.extended_path(path), ignore_errors=True)


def closed_external_contract_fixture(
    root: Path, code_path: str = "feature.txt", code_content: str = "feature\n"
):
    code_repo = root / "repo-a"
    code_base = init_repo(code_repo, "main")
    memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
    memory_seed = init_repo(memory_repo, "main")
    write_ledger(memory_repo / "memory.md", create_initial_ledger("repo-a", code_base, memory_seed))
    git(memory_repo, "add", "memory.md")
    git(memory_repo, "commit", "-m", "Add memory ledger")
    memory_base = git(memory_repo, "rev-parse", "HEAD")
    parent, code_master, memory_master = _series_parent_fixture(
        root,
        task_name="Integrate Thing",
        code_repo=code_repo,
        memory_repo=memory_repo,
    )
    contract = default_contract(
        ContractTask(
            name="Integrate Thing",
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="chat-task",
            memory_mode="external",
        ),
        leaf=LeafIdentity(worktree_name="integrate-thing", lifecycle_id="LC-INTEGRATE-THING"),
        code=RepoBranchPlan(
            repo_path=code_master,
            source_branch=parent.code_work_branch,
            work_branch="ar/integrate-thing-leaf",
            base_commit=code_base,
        ),
        memory=RepoBranchPlan(
            repo_path=memory_master,
            source_branch=parent.memory_work_branch,
            work_branch="ar/integrate-thing-leaf",
            base_commit=memory_base,
        ),
    )
    assert contract.memory_worktree is not None
    git(
        code_master,
        "worktree",
        "add",
        "-b",
        contract.code_work_branch,
        str(contract.code_worktree),
        parent.code_work_branch,
    )
    git(
        memory_master,
        "worktree",
        "add",
        "-b",
        contract.memory_work_branch,
        str(contract.memory_worktree),
        parent.memory_work_branch,
    )
    code_commit = commit_file(contract.code_worktree, code_path, code_content, "Add feature")
    memory_content_commit = commit_file(
        contract.memory_worktree, "onboarding/feature.txt.md", "# feature\n", "Document feature"
    )
    ledger_commit = commit_memory_ledger(
        contract.memory_worktree, code_commit, memory_content_commit, "Sync ledger"
    )
    closed = replace(
        contract,
        parent_contract_path=parent.contract_path,
        human_review_status="approved",
        approved_for_commit=True,
        closeout_status="completed",
        code_commit=code_commit,
        memory_content_commit=memory_content_commit,
        ledger_commit=ledger_commit,
    )
    write_contract(closed.contract_path, closed)
    write_passing_route_review(closed)
    return closed


def closeout_args(contract, *, dry_run: bool = False) -> Namespace:
    return Namespace(
        contract_path=contract.contract_path,
        approved=not dry_run,
        approval_note="" if dry_run else "developer approved commit preview",
        code_commit_message="Add feature",
        memory_commit_message="Document feature",
        ledger_commit_message="Sync ledger",
        dry_run=dry_run,
        operation_progress=None if dry_run else MutationEvidenceRecorder(),
    )


def run_authorized_closeout_mechanics(args: Namespace) -> int:
    """Exercise commit mechanics with explicit test evidence authority, never the CLI."""
    worktree_args = worktree_manager.WorktreeArgs.from_namespace(args)
    if worktree_args.dry_run:
        return worktree_manager.command_closeout(args)
    if worktree_args.operation_progress is None:
        raise AssertionError("applying closeout mechanics require an evidence recorder")
    assert worktree_args.contract_path is not None
    contract = load_contract(worktree_args.contract_path)
    effective = normalize_closeout_input(
        contract,
        raw_closeout_messages(
            code=getattr(args, "code_commit_message", None),
            memory=getattr(args, "memory_commit_message", None),
            ledger=getattr(args, "ledger_commit_message", None),
        ),
        route="worktree",
        corrected_call=CloseoutCorrectedCall(
            tool="worktree_closeout_apply",
            arguments={"contract_path": contract.contract_path.as_posix()},
        ),
    )
    result = worktree_manager.closeout_result(
        replace(worktree_args, closeout_input=effective, ledger_commit_message=""),
        contract,
    )
    print(json.dumps(result.payload, indent=2))
    return result.returncode


def integrate_args(contract, *, dry_run: bool = False) -> Namespace:
    return Namespace(
        contract_path=contract.contract_path,
        approved=not dry_run,
        strategy="ff-only",
        ledger_commit_message="",
        dry_run=dry_run,
    )


def integrated_external_contract_fixture(root: Path):
    contract = dirty_open_external_contract_fixture(root)
    with redirect_stdout(io.StringIO()):
        assert run_authorized_closeout_mechanics(closeout_args(contract)) == 0
    closed = load_contract(contract.contract_path)
    assert closed.memory_repo_path is not None
    git(
        closed.code_repo_path,
        "update-ref",
        f"refs/heads/{closed.code_source_branch}",
        closed.code_commit,
    )
    git(
        closed.memory_repo_path,
        "update-ref",
        f"refs/heads/{closed.memory_source_branch}",
        closed.ledger_commit,
    )
    integrated = replace(
        closed,
        integration_status="completed",
        integration_strategy="ff-only",
        integrated_code_commit=closed.code_commit,
        integrated_memory_content_commit=closed.memory_content_commit,
        integrated_ledger_commit=closed.ledger_commit,
    )
    write_contract(integrated.contract_path, integrated)
    # This helper returns a candidate intended for a fresh direct-mechanics invocation. The
    # preceding closeout changed the memory candidate, so publish the exact-current coherence
    # authority that the shared preflight now requires before returning it.
    write_passing_route_review(integrated)
    return integrated


class WorktreeSupportTests(unittest.TestCase):
    def _external_memory_skeleton(self, root: Path) -> tuple[Path, Path]:
        """Build an external-memory code+memory skeleton; return (code_repo, coordination_root)."""
        code_repo = root / "repo-a"
        code_repo.mkdir()
        coordination_root = root / "ar-coordination"
        memory_repo = coordination_root / "memory-repos" / "ar-repo-a"
        (memory_repo / "system").mkdir(parents=True)
        (memory_repo / "onboarding").mkdir()
        (memory_repo / "system" / "settings.md").write_text("# Settings\n", encoding="utf-8")
        return code_repo, coordination_root

    def _write_task_contract(
        self, coordination_root: Path, code_repo: Path, *, task_name: str, worktree_name: str
    ):
        """Write a real external-memory contract at tasks/<repo>/<task>/contract.md."""
        contract = default_contract(
            ContractTask(
                name=task_name,
                repo_name="repo-a",
                coordination_root=coordination_root,
                workflow_kind="light-task",
                memory_mode="external",
            ),
            leaf=LeafIdentity(worktree_name=worktree_name),
            code=RepoBranchPlan(
                repo_path=code_repo,
                source_branch="main",
                work_branch=f"ar/{worktree_name}",
                base_commit="abc123",
            ),
            memory=RepoBranchPlan(
                repo_path=coordination_root / "memory-repos" / "ar-repo-a",
                source_branch="main",
                work_branch=f"ar/{worktree_name}",
                base_commit="def456",
            ),
        )
        write_contract(contract.contract_path, contract)
        return contract

    def _unmapped_external_contract(self, root: Path):
        """A contract whose code base commit is NOT in the memory ledger -> the missing-mapping block."""
        code_repo = root / "repo-a"
        c1 = init_repo(code_repo, "main")
        # A code-only commit on top: a new SHA the ledger has never seen (the F-R scenario).
        unmapped = commit_file(code_repo, "feature.py", "value = 1\n", "code-only change")
        self.assertNotEqual(unmapped, c1)
        memory_repo = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
        memory_head = initialized_memory_repo(memory_repo, "repo-a", "main", "main", c1)
        # The ledger's memory CONTENT tip (the README commit init_repo made) -- distinct from the
        # ledger-commit HEAD; reconciliation maps the unmapped code base to THIS, leaving it unchanged.
        content_commit = load_ledger(memory_repo / "memory.md").last_memory_content_commit
        contract = default_contract(
            ContractTask(
                name="Fix Thing",
                repo_name="repo-a",
                coordination_root=root / "ar-coordination",
                workflow_kind="light-task",
                memory_mode="external",
            ),
            leaf=LeafIdentity(worktree_name="fix-thing"),
            code=RepoBranchPlan(
                repo_path=code_repo,
                source_branch="main",
                work_branch="ar/fix-thing",
                base_commit=unmapped,
            ),
            memory=RepoBranchPlan(
                repo_path=memory_repo,
                source_branch="main",
                work_branch="ar/fix-thing",
                base_commit=memory_head,
            ),
        )
        return contract, memory_repo, unmapped, content_commit


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
