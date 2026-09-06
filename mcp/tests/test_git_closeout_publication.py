"""One real expected-old closeout CAS, distinct from private preparation authority."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.kernel import git_command
from agents_remember.kernel.git_closeout_publication import (
    GitCloseoutPublicationBinding,
    GitCloseoutPublicationCapability,
    GitCloseoutPublicationError,
)
from agents_remember.kernel.git_command import (
    admit_git_closeout_publication,
    admit_private_git_preparation,
    closeout_publication_command,
    inspect_git_closeout_publication,
    inspect_git_preparation,
    publish_git_closeout_ref,
    read_git_blob_bytes,
    read_git_tree_bytes,
    run_git,
    run_git_preparation,
)
from agents_remember.kernel.git_preparation import PrivateGitPreparationBinding
from test_git_command import _commit, _init


@dataclass
class _Publication:
    binding: GitCloseoutPublicationBinding
    calls: list[GitCloseoutPublicationBinding] = field(default_factory=list)
    allowed: bool = True

    def authorize(self, actual: GitCloseoutPublicationBinding) -> None:
        self.calls.append(actual)
        if not self.allowed or actual != self.binding:
            raise RuntimeError("actual publication owner no longer authorized")

    def capability(self) -> GitCloseoutPublicationCapability:
        return admit_git_closeout_publication(self.binding, authorize=self.authorize)


@pytest.fixture
def publication(tmp_path: Path) -> _Publication:
    root = tmp_path / "logical"
    _init(root)
    old = _commit(root, "source.txt", "before\n")
    (root / "source.txt").write_text("prepared\n", encoding="utf-8")
    (root / "line\r\nname.bin").write_bytes(b"original\r\nblob\x00bytes")
    assert run_git(root, ["add", "-A"]).returncode == 0
    tree = run_git(root, ["write-tree"]).stdout.strip()
    binding = PrivateGitPreparationBinding(
        root,
        "refs/heads/main",
        old,
        root / ".git",
        tmp_path / "prepared",
        old,
        tree,
        "prepare actual output",
        "strict-code-no-verify",
        "operation",
        1,
        "a" * 64,
    )
    private = admit_private_git_preparation(binding, authorize=lambda _binding: None)
    for action in ("create", "materialize", "commit"):
        assert run_git_preparation(private, action).returncode == 0
    observed = inspect_git_preparation(private)
    assert observed.head is not None
    return _Publication(
        GitCloseoutPublicationBinding(
            root,
            "refs/heads/main",
            root / ".git",
            old,
            observed.head,
            tree,
            "operation",
            1,
            "b" * 64,
        )
    )


def _state(binding: GitCloseoutPublicationBinding) -> tuple[str, str, bytes, bytes, bytes]:
    root = binding.root
    return (
        run_git(root, ["symbolic-ref", "HEAD"]).stdout,
        run_git(root, ["rev-parse", binding.logical_ref]).stdout,
        (root / ".git/index").read_bytes(),
        (root / "source.txt").read_bytes(),
        (root / "line\r\nname.bin").read_bytes(),
    )


def test_one_real_cas_preserves_index_body_and_already_new_does_not_write(
    publication: _Publication,
) -> None:
    capability = publication.capability()
    before = _state(publication.binding)
    plan = closeout_publication_command(publication.binding)
    with mock.patch.object(subprocess, "run", wraps=subprocess.run) as spawned:
        result = publish_git_closeout_ref(capability)
    assert result.command is not None and result.command.returncode == 0
    assert (result.before.state, result.after.state) == ("old", "new")
    assert result.after.commit == publication.binding.prepared_commit
    assert plan.args == (
        "update-ref",
        publication.binding.logical_ref,
        publication.binding.prepared_commit,
        publication.binding.expected_old_commit,
    )
    assert [call.args[0] for call in spawned.call_args_list].count(list(plan.argv)) == 1
    after = _state(publication.binding)
    assert after[0] == before[0] and after[2:] == before[2:]
    assert after[1].strip() == publication.binding.prepared_commit
    with mock.patch.object(subprocess, "run", wraps=subprocess.run) as reopened:
        repeated = publish_git_closeout_ref(publication.capability())
    assert repeated.command is None and repeated.before == repeated.after == result.after
    assert all("update-ref" not in call.args[0] for call in reopened.call_args_list)
    assert _state(publication.binding) == after
    assert len(publication.calls) >= 6


def test_exact_binary_blob_and_tree_readers_preserve_crlf_paths(publication: _Publication) -> None:
    raw = read_git_tree_bytes(publication.binding.root, publication.binding.prepared_tree)
    row = next(row for row in raw.split(b"\0") if row.endswith(b"\tline\r\nname.bin"))
    blob_id = row.split(b" ", 2)[2].split(b"\t", 1)[0].decode("ascii")
    assert read_git_blob_bytes(publication.binding.root, blob_id) == b"original\r\nblob\x00bytes"
    assert b"\tline\nname.bin\0" not in raw
    for reader in (read_git_tree_bytes, read_git_blob_bytes):
        with pytest.raises(ValueError, match="complete object identity"):
            reader(publication.binding.root, "HEAD")


@pytest.mark.parametrize("fault", ["body", "index", "hidden", "untracked", "foreign-ref"])
def test_late_source_or_ref_movement_refuses_without_overwriting(
    publication: _Publication, fault: str
) -> None:
    capability = publication.capability()
    root = publication.binding.root
    if fault == "body":
        (root / "source.txt").write_text("late body\n", encoding="utf-8")
    elif fault == "index":
        assert run_git(root, ["read-tree", publication.binding.expected_old_commit]).returncode == 0
    elif fault == "hidden":
        assert run_git(root, ["update-index", "--assume-unchanged", "source.txt"]).returncode == 0
    elif fault == "untracked":
        (root / "late-file.txt").write_text("preserve", encoding="utf-8")
    else:
        assert run_git(root, ["commit", "-m", "foreign owner commit"]).returncode == 0
    before = _state(publication.binding)
    with (
        mock.patch.object(subprocess, "run", wraps=subprocess.run) as spawned,
        pytest.raises(GitCloseoutPublicationError),
    ):
        publish_git_closeout_ref(capability)
    assert all("update-ref" not in call.args[0] for call in spawned.call_args_list)
    assert _state(publication.binding) == before
    if fault == "untracked":
        assert (root / "late-file.txt").read_text() == "preserve"


def test_cancelled_and_unminted_authority_cannot_publish(publication: _Publication) -> None:
    capability = publication.capability()
    forged = GitCloseoutPublicationCapability(publication.binding, publication.authorize, object())
    before = _state(publication.binding)
    with pytest.raises(GitCloseoutPublicationError, match="not admitted"):
        publish_git_closeout_ref(forged)
    publication.allowed = False
    with pytest.raises(RuntimeError, match="no longer authorized"):
        publish_git_closeout_ref(capability)
    assert _state(publication.binding) == before


def test_wrong_prepared_tree_and_non_direct_parent_are_not_publishable(
    publication: _Publication, tmp_path: Path
) -> None:
    before = _state(publication.binding)
    wrong_tree = run_git(publication.binding.root, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
    bad_tree = replace(publication.binding, prepared_tree=wrong_tree)
    with pytest.raises(GitCloseoutPublicationError, match="tree differs"):
        admit_git_closeout_publication(bad_tree, authorize=lambda _binding: None)
    private = tmp_path / "prepared"
    _commit(private, "later.txt", "second descendant\n")
    descendant = run_git(private, ["rev-parse", "HEAD"]).stdout.strip()
    tree = run_git(private, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
    bad_parent = replace(publication.binding, prepared_commit=descendant, prepared_tree=tree)
    with pytest.raises(GitCloseoutPublicationError, match="sole expected-old parent"):
        admit_git_closeout_publication(bad_parent, authorize=lambda _binding: None)
    assert _state(publication.binding) == before


def test_existing_leg_is_observation_only(publication: _Publication) -> None:
    assert publish_git_closeout_ref(publication.capability()).after.state == "new"
    publication.binding = replace(
        publication.binding, expected_old_commit=publication.binding.prepared_commit
    )
    before = _state(publication.binding)
    with pytest.raises(GitCloseoutPublicationError, match="observation-only"):
        closeout_publication_command(publication.binding)
    with mock.patch.object(subprocess, "run", wraps=subprocess.run) as spawned:
        result = publish_git_closeout_ref(publication.capability())
    assert result.command is None and result.before.state == result.after.state == "existing"
    assert all("update-ref" not in call.args[0] for call in spawned.call_args_list)
    assert _state(publication.binding) == before


def test_real_ref_lock_failure_is_retained_and_lost_response_reopens_exact_new_ref(
    publication: _Publication, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = publication.capability()
    before = _state(publication.binding)
    lock = publication.binding.common_directory / "refs/heads/main.lock"
    lock.write_bytes(b"another actual ref writer")
    result = publish_git_closeout_ref(capability)
    assert result.command is not None and result.command.returncode != 0
    assert result.before == result.after and result.after.state == "old"
    assert (
        lock.read_bytes() == b"another actual ref writer" and _state(publication.binding) == before
    )
    lock.unlink()
    original = git_command.run_git

    def lose_response(root, args, **kwargs):
        actual = original(root, args, **kwargs)
        if args[0] == "update-ref":
            assert actual.returncode == 0
            raise subprocess.TimeoutExpired(args, 300)
        return actual

    monkeypatch.setattr(git_command, "run_git", lose_response)
    with pytest.raises(subprocess.TimeoutExpired):
        publish_git_closeout_ref(capability)
    assert inspect_git_closeout_publication(capability).state == "new"
    repeated = publish_git_closeout_ref(capability)
    assert repeated.command is None and repeated.after.state == "new"
    assert _state(publication.binding)[2:] == before[2:]
