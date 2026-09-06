from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from unittest import mock

from agents_remember.memory_quality.style.citations import (
    fixer,
    model,
    source_index,
    source_index_database,
    source_index_state,
    symbol_index,
)
from agents_remember.memory_quality.style.citations.candidate import (
    git_source as source_index_candidate,
)
from agents_remember.memory_quality.style.citations.resolution import Trees
from test_memory_citation_source_index import IndexCase


class SnapshotReuseTests(IndexCase):
    def test_expected_snapshot_opens_without_tree_or_integrity_traversal(self) -> None:
        self.write("one.py", "def alpha():\n    return 1\n")
        _cold, snapshot = self.acquire()

        with (
            mock.patch.object(
                source_index,
                "_tree_state",
                side_effect=AssertionError("frozen acquisition inspected the source tree"),
            ),
            mock.patch.object(
                source_index,
                "_reclaim_legacy_cache_roots",
                side_effect=AssertionError("frozen acquisition ran cache reclamation"),
            ),
            mock.patch.object(
                source_index_database.Database,
                "validate_application_integrity",
                side_effect=AssertionError("frozen acquisition traversed database integrity"),
            ),
            mock.patch.object(
                source_index.Manifest,
                "from_json",
                side_effect=AssertionError("frozen acquisition parsed the full manifest"),
            ),
            source_index.open_repository_index(self.trees, expected_snapshot=snapshot) as index,
        ):
            telemetry = index.telemetry()

        self.assertEqual(telemetry["state"], "frozen")
        self.assertEqual(telemetry["snapshotId"], snapshot)
        for key in (
            "metadataFilesStat",
            "metadataDirectoriesStat",
            "metadataEntriesEnumerated",
            "metadataTreeEnumerations",
            "sourceFilesRead",
            "sourceBytesRead",
            "sourceFilesTokenized",
            "sourceFilesParsed",
            "buildSeconds",
        ):
            self.assertEqual(telemetry[key], 0)

    def test_expected_snapshot_refuses_missing_wrong_and_replaced_generations(self) -> None:
        self.write("one.py", "alpha = 1\n")
        missing = "0" * 64
        with self.assertRaisesRegex(source_index.SourceIndexError, "not published"):
            source_index.open_repository_index(self.trees, expected_snapshot=missing)

        _cold, first = self.acquire()
        with self.assertRaisesRegex(source_index.SourceIndexError, "unavailable"):
            source_index.open_repository_index(self.trees, expected_snapshot="f" * 64)

        self.write("one.py", "beta = 2\n")
        _rebuilt, second = self.acquire()
        self.assertNotEqual(second, first)
        with (
            mock.patch.object(
                source_index,
                "_tree_state",
                side_effect=AssertionError("mismatch must not fall back to source validation"),
            ),
            self.assertRaisesRegex(source_index.SourceIndexError, "unavailable"),
        ):
            source_index.open_repository_index(self.trees, expected_snapshot=first)

    def test_expected_snapshot_requires_canonical_id_before_any_cache_or_source_work(self) -> None:
        for malformed in ("", "a" * 63, "A" * 64, "g" * 64, "a" * 65):
            with (
                self.subTest(malformed=malformed),
                mock.patch.object(
                    source_index,
                    "cache_paths",
                    side_effect=AssertionError("malformed id reached cache acquisition"),
                ),
                mock.patch.object(
                    source_index,
                    "_tree_state",
                    side_effect=AssertionError("malformed id inspected source"),
                ),
                self.assertRaisesRegex(source_index.SourceIndexError, "64 lowercase hex"),
            ):
                source_index.open_repository_index(self.trees, expected_snapshot=malformed)

    def test_readiness_marker_and_database_metadata_fail_closed_without_fallback(self) -> None:
        self.write("one.py", "alpha = 1\n")
        _cold, snapshot = self.acquire()
        paths = source_index.cache_paths(self.trees)
        original = paths.readiness.read_text(encoding="utf-8")
        payload = json.loads(original)
        corruptions = (
            {**payload, "schemaVersion": -1},
            {**payload, "state": "building"},
            {**payload, "snapshotId": snapshot.upper()},
            {**payload, "filesIndexed": -1},
            {**payload, "sourceBytes": source_index.MAX_SOURCE_BYTES + 1},
            {**payload, "databaseBytes": 0},
            {**payload, "codeRoot": "relative/code"},
            {**payload, "extra": True},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                paths.readiness.write_text(json.dumps(corruption), encoding="utf-8")
                self.frozen_refusal_without_discovery(snapshot)
        paths.readiness.write_bytes(b"x" * (source_index_state.MAX_READINESS_BYTES + 1))
        self.frozen_refusal_without_discovery(snapshot)
        paths.readiness.write_text(original, encoding="utf-8")
        with closing(sqlite3.connect(paths.database)) as connection:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'application_sha256'", ("bad",)
            )
            connection.commit()
        self.frozen_refusal_without_discovery(snapshot)

    def test_old_readiness_cannot_bless_a_replaced_database(self) -> None:
        target = self.write("one.py", "alpha = 1\n")
        _cold, first = self.acquire()
        paths = source_index.cache_paths(self.trees)
        old_readiness = paths.readiness.read_bytes()
        target.write_text("beta = 2\n", encoding="utf-8")
        _rebuilt, second = self.acquire()
        self.assertNotEqual(first, second)
        paths.readiness.write_bytes(old_readiness)

        self.assertIn("identity do not match", self.frozen_refusal_without_discovery(first))

    def test_warm_process_reads_and_derives_no_source(self) -> None:
        self.write("one.py", "def alpha():\n    return 1\n")
        cold, snapshot = self.acquire()
        warm, same_snapshot = self.acquire()

        self.assertEqual(cold["state"], "built")
        self.assertEqual(warm["state"], "warm")
        self.assertEqual(same_snapshot, snapshot)
        for key in (
            "sourceFilesRead",
            "sourceBytesRead",
            "sourceFilesTokenized",
            "sourceFilesParsed",
        ):
            self.assertEqual(warm[key], 0)
        self.assertEqual(warm["metadataTreeEnumerations"], 1)

    def test_memory_edit_does_not_invalidate_code_snapshot(self) -> None:
        self.write("one.py", "def alpha():\n    return 1\n")
        _cold, snapshot = self.acquire()
        (self.memory / "card.md").write_text("changed memory\n", encoding="utf-8")

        warm, same_snapshot = self.acquire()

        self.assertEqual(warm["state"], "warm")
        self.assertEqual(same_snapshot, snapshot)

    def test_metadata_only_touch_rehashes_one_file_without_retokenizing(self) -> None:
        target = self.write("one.py", "def alpha():\n    return 1\n")
        _cold, snapshot = self.acquire()
        os.utime(target, None)

        refreshed, same_snapshot = self.acquire()
        warm, final_snapshot = self.acquire()

        self.assertEqual(refreshed["state"], "metadata-refreshed")
        self.assertEqual(refreshed["sourceFilesRead"], 1)
        self.assertEqual(refreshed["sourceFilesTokenized"], 0)
        self.assertEqual(refreshed["sourceFilesParsed"], 0)
        self.assertEqual((same_snapshot, final_snapshot), (snapshot, snapshot))
        self.assertEqual(warm["sourceFilesRead"], 0)

    def test_same_size_restored_mtime_content_change_rebuilds(self) -> None:
        target = self.write("one.py", "old_name = 1\n")
        original = target.stat()
        _cold, snapshot = self.acquire()
        target.write_text("new_name = 1\n", encoding="utf-8")
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

        rebuilt, changed_snapshot = self.acquire()

        self.assertEqual(rebuilt["state"], "built")
        self.assertNotEqual(changed_snapshot, snapshot)
        anchor = model.Anchor(model.SYMBOL, "new_name")
        with source_index.open_repository_index(self.trees) as index:
            self.assertEqual(
                symbol_index.locate((anchor,), self.trees, index=index)[anchor].files, 1
            )

    def test_add_delete_and_rename_each_rebuild_once(self) -> None:
        target = self.write("one.py", "one = 1\n")
        _cold, first = self.acquire()
        added = self.write("two.py", "two = 2\n")
        add, second = self.acquire()
        warm_after_add, _ = self.acquire()
        target.unlink()
        delete, third = self.acquire()
        added.rename(self.code / "renamed.py")
        rename, fourth = self.acquire()

        self.assertEqual([add["state"], delete["state"], rename["state"]], ["built"] * 3)
        self.assertEqual(warm_after_add["state"], "warm")
        self.assertEqual(len({first, second, third, fourth}), 4)

    def test_warm_fix_and_post_fix_recheck_share_one_index_lease(self) -> None:
        source = self.write("one.py", "# preface\ndef target():\n    return 1\n")
        onboarding = self.memory / "onboarding"
        onboarding.mkdir()
        card = onboarding / "one.py.md"
        card.write_text(
            "# one.py\n\n"
            "| Field | Value |\n| --- | --- |\n| repository | test |\n"
            "| path | `one.py` |\n\n"
            "## Repo-Internal References\n\n"
            "| Finding | Anchor | Source |\n| --- | --- | --- |\n"
            "| Target definition. | `target` | one.py:1-1 |\n",
            encoding="utf-8",
        )
        _built, snapshot = self.acquire()
        reads: list[Path] = []
        original_read_text = Path.read_text

        def read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads.append(path)
            return original_read_text(path, encoding=encoding, errors=errors)

        with (
            mock.patch.object(Path, "read_text", new=read_text),
            mock.patch.object(
                source_index,
                "_tree_state",
                side_effect=AssertionError("frozen fixer inspected the source tree"),
            ),
        ):
            result = fixer.fix_onboarding_root(
                onboarding,
                self.code,
                dry_run=True,
                only="one.py.md",
                expected_snapshot=snapshot,
            )

        telemetry = result["sourceIndex"]
        self.assertEqual(result["failingClaims"], 1)
        self.assertEqual(telemetry["state"], "frozen")
        self.assertEqual(telemetry["sourceFilesRead"], 0)
        self.assertEqual(telemetry["sourceFilesTokenized"], 0)
        self.assertEqual(telemetry["sourceFilesParsed"], 0)
        self.assertEqual(telemetry["metadataTreeEnumerations"], 0)
        self.assertEqual(telemetry["metadataFilesStat"], 0)
        self.assertEqual(telemetry["metadataDirectoriesStat"], 0)
        self.assertEqual(telemetry["indexQueries"], 2)
        self.assertEqual(telemetry["directAnchorQueries"], 2)
        self.assertTrue(telemetry["postFixRecheck"]["reusedLease"])
        self.assertIn(card, reads)
        self.assertIn(source, reads)


class CandidateSourceSnapshotTests(IndexCase):
    def setUp(self) -> None:
        super().setUp()
        self.primary = self.code
        self._git("init", "-q")
        self._git("config", "user.email", "candidate-index@test.invalid")
        self._git("config", "user.name", "candidate index test")
        self.write(".gitignore", "generated/\n")
        self.write("pkg/source.py", "def candidate_symbol():\n    return 1\n")
        self.write("pkg/deleted.py", "deleted_symbol = 1\n")
        self.write("pkg/renamed.py", "renamed_symbol = 1\n")
        self.write("generated/tracked.py", "tracked_ignored_symbol = 1\n")
        self._git("add", "-f", ".gitignore", "pkg", "generated/tracked.py")
        self._git("commit", "-qm", "candidate source baseline")
        self.tree = self._git("rev-parse", "HEAD^{tree}")
        linked = self.root / "linked"
        self._git("worktree", "add", "--detach", "-q", str(linked), "HEAD")
        self.code = linked
        self.trees = Trees(self.code, self.memory)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.code,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def _candidate_trees(self, tree: str | None = None) -> Trees:
        return Trees(self.code, self.memory, candidate_tree=self.tree if tree is None else tree)

    def test_linked_candidate_excludes_generated_competitors_but_retains_tracked_ignored_files(
        self,
    ) -> None:
        self.assertTrue((self.code / ".git").is_file())
        self.write("generated/output.js", "function candidate_symbol() { return 2; }\n")
        self.write("new.py", "new_untracked_symbol = 1\n")
        candidate = self._candidate_trees()
        expected = {
            ".gitignore",
            "pkg/source.py",
            "pkg/deleted.py",
            "pkg/renamed.py",
            "generated/tracked.py",
        }
        self.assertEqual({name for _, name in source_index.code_files(candidate)}, expected)
        anchor = model.Anchor(model.SYMBOL, "candidate_symbol")
        tracked = model.Anchor(model.SYMBOL, "tracked_ignored_symbol")
        with source_index.open_repository_index(candidate) as index:
            seen = symbol_index.locate((anchor, tracked), candidate, index=index)
            self.assertEqual([one.path for one in seen[anchor].locations], ["pkg/source.py"])
            self.assertEqual(
                [one.path for one in seen[tracked].locations], ["generated/tracked.py"]
            )
            self.assertIsNone(candidate.resolve("generated/output.js"))
            self.assertIsNone(candidate.resolve("new.py"))
            self.assertIsNone(candidate.resolve(".git"))
        with source_index.open_repository_index(self.trees) as ordinary:
            seen = symbol_index.locate((anchor,), self.trees, index=ordinary)
            self.assertEqual(
                {one.path for one in seen[anchor].locations},
                {"pkg/source.py", "generated/output.js"},
            )
            fresh = model.Anchor(model.SYMBOL, "new_untracked_symbol")
            self.assertEqual(
                [
                    one.path
                    for one in symbol_index.locate((fresh,), self.trees, index=ordinary)[
                        fresh
                    ].locations
                ],
                ["new.py"],
            )
        with source_index.open_repository_index(candidate) as reacquired:
            self.assertEqual(reacquired.files_indexed, len(expected))
            self.assertEqual(
                [
                    one.path
                    for one in symbol_index.locate((anchor,), candidate, index=reacquired)[
                        anchor
                    ].locations
                ],
                ["pkg/source.py"],
            )

    def test_staged_add_delete_and_rename_define_the_candidate_without_moving_head(self) -> None:
        head = self._git("rev-parse", "HEAD")
        self._git("rm", "pkg/deleted.py")
        self._git("mv", "pkg/renamed.py", "pkg/moved.py")
        self.write("pkg/added.py", "new_staged_symbol = 1\n")
        self._git("add", "pkg/added.py")
        staged_tree = self._git("write-tree")
        self.assertNotEqual(staged_tree, self.tree)
        candidate = self._candidate_trees(staged_tree)
        with source_index.open_repository_index(candidate) as index:
            anchors = tuple(
                model.Anchor(model.SYMBOL, name)
                for name in ("new_staged_symbol", "renamed_symbol", "deleted_symbol")
            )
            seen = symbol_index.locate(anchors, candidate, index=index)
            self.assertEqual([one.path for one in seen[anchors[0]].locations], ["pkg/added.py"])
            self.assertEqual([one.path for one in seen[anchors[1]].locations], ["pkg/moved.py"])
            self.assertEqual(seen[anchors[2]].files, 0)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)

    def test_frozen_acquisition_refuses_a_different_policy_with_identical_eligible_files(
        self,
    ) -> None:
        ordinary = Trees(self.primary, self.memory)
        candidate = Trees(self.primary, self.memory, candidate_tree=self.tree)
        self.assertEqual(source_index.code_files(ordinary), source_index.code_files(candidate))
        with source_index.open_repository_index(ordinary) as index:
            ordinary_snapshot = index.snapshot_id
        with (
            mock.patch.object(source_index, "_tree_state", side_effect=AssertionError("walk")),
            mock.patch.object(
                source_index, "_build_and_publish", side_effect=AssertionError("build")
            ),
            self.assertRaises(source_index.SourceIndexError),
        ):
            source_index.open_repository_index(candidate, expected_snapshot=ordinary_snapshot)
        with source_index.open_repository_index(candidate) as index:
            candidate_snapshot = index.snapshot_id
        with (
            mock.patch.object(source_index, "_tree_state", side_effect=AssertionError("walk")),
            mock.patch.object(
                source_index, "_build_and_publish", side_effect=AssertionError("build")
            ),
            self.assertRaises(source_index.SourceIndexError),
        ):
            source_index.open_repository_index(ordinary, expected_snapshot=candidate_snapshot)

    def test_frozen_acquisition_binds_exact_tree_even_when_indexed_content_is_identical(
        self,
    ) -> None:
        first = self._candidate_trees()
        with source_index.open_repository_index(first) as index:
            snapshot = index.snapshot_id
        (self.code / "image.png").write_bytes(b"excluded image fixture")
        self._git("add", "image.png")
        second_tree = self._git("write-tree")
        second = self._candidate_trees(second_tree)
        self.assertNotEqual(second_tree, self.tree)
        self.assertEqual(source_index.code_files(first), source_index.code_files(second))
        with (
            mock.patch.object(source_index, "_tree_state", side_effect=AssertionError("walk")),
            mock.patch.object(
                source_index, "_build_and_publish", side_effect=AssertionError("build")
            ),
            self.assertRaises(source_index.SourceIndexError),
        ):
            source_index.open_repository_index(second, expected_snapshot=snapshot)
        with (
            mock.patch.object(source_index, "_tree_state", side_effect=AssertionError("walk")),
            source_index.open_repository_index(first, expected_snapshot=snapshot) as frozen,
        ):
            self.assertEqual(frozen.telemetry()["state"], "frozen")
            self.assertEqual(frozen.telemetry()["metadataTreeEnumerations"], 0)

    def test_candidate_acquisition_refuses_dirty_missing_and_unsafe_source_nodes(self) -> None:
        candidate = self._candidate_trees()
        target = self.code / "pkg/source.py"
        original = target.read_bytes()
        with source_index.open_repository_index(candidate) as index:
            ready = index.paths.readiness
            ready_bytes = ready.read_bytes()
        outside = self.root / "outside.py"
        outside.write_bytes(original)
        for fault in ("dirty", "missing", "file-symlink", "parent-symlink"):
            with self.subTest(fault=fault):
                if fault == "dirty":
                    target.write_text("def candidate_symbol():\n    return 2\n", encoding="utf-8")
                elif fault == "missing":
                    target.unlink()
                elif fault == "file-symlink":
                    target.unlink()
                    target.symlink_to(outside)
                else:
                    target.parent.rename(self.root / "outside-package")
                    target.parent.symlink_to(
                        self.root / "outside-package", target_is_directory=True
                    )
                try:
                    with self.assertRaises(source_index.SourceIndexError):
                        source_index.open_repository_index(candidate)
                    with self.assertRaises(ValueError):
                        candidate.resolve("pkg/source.py")
                    self.assertEqual(ready.read_bytes(), ready_bytes)
                finally:
                    if fault == "parent-symlink":
                        target.parent.unlink()
                        (self.root / "outside-package").rename(target.parent)
                    elif target.is_symlink():
                        target.unlink()
                    target.write_bytes(original)

    def test_invalid_candidate_tree_refuses_before_cache_acquisition(self) -> None:
        for tree in ("HEAD", "a" * 39, "A" * 40, "g" * 40, "a" * 41):
            with (
                self.subTest(tree=tree),
                mock.patch.object(source_index, "cache_paths", side_effect=AssertionError("cache")),
                self.assertRaises(ValueError),
            ):
                source_index.open_repository_index(self._candidate_trees(tree))

    def test_candidate_keeps_tracked_source_under_default_skipped_directories(self) -> None:
        for directory in ("build", "dist", "node_modules"):
            self.write(f"{directory}/owned.py", f"{directory}_owned_symbol = 1\n")
        self._git("add", "-f", "build", "dist", "node_modules")
        candidate = self._candidate_trees(self._git("write-tree"))
        with source_index.open_repository_index(candidate) as index:
            for directory in ("build", "dist", "node_modules"):
                anchor = model.Anchor(model.SYMBOL, f"{directory}_owned_symbol")
                observed = symbol_index.locate((anchor,), candidate, index=index)[anchor]
                self.assertEqual(
                    [one.path for one in observed.locations], [f"{directory}/owned.py"]
                )
        ordinary_paths = {relative for _, relative in source_index.code_files(self.trees)}
        self.assertFalse(any(path.endswith("/owned.py") for path in ordinary_paths))

    def test_candidate_excludes_its_nested_memory_from_the_symbol_population(self) -> None:
        nested_memory = self.code / "local-memory"
        nested_memory.mkdir()
        (nested_memory / "card.md").write_text("candidate_symbol is explained here\n")
        self._git("add", "local-memory/card.md")
        candidate = Trees(self.code, nested_memory, candidate_tree=self._git("write-tree"))
        anchor = model.Anchor(model.SYMBOL, "candidate_symbol")
        with source_index.open_repository_index(candidate) as index:
            locations = symbol_index.locate((anchor,), candidate, index=index)[anchor].locations
            self.assertEqual([one.path for one in locations], ["pkg/source.py"])
        self.assertNotIn(
            "local-memory/card.md", {relative for _, relative in source_index.code_files(candidate)}
        )

    def test_candidate_refuses_missing_tree_commit_identity_and_nested_git_root(self) -> None:
        invalid = (
            self._candidate_trees("0" * 40),
            self._candidate_trees(self._git("rev-parse", "HEAD")),
            Trees(self.code / "pkg", self.memory, candidate_tree=self.tree),
        )
        for trees in invalid:
            with (
                self.subTest(root=trees.code_root, tree=trees.candidate_tree),
                self.assertRaisesRegex(source_index.SourceIndexError, "candidate"),
            ):
                source_index.open_repository_index(trees)

    def test_candidate_detects_changed_bytes_even_with_restored_size_and_mtime(self) -> None:
        candidate = self._candidate_trees()
        target = self.code / "pkg/source.py"
        original = target.stat()
        with source_index.open_repository_index(candidate):
            pass
        target.write_text("def candidate_symbol():\n    return 2\n")
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(target.stat().st_size, original.st_size)
        self.assertEqual(target.stat().st_mtime_ns, original.st_mtime_ns)
        with self.assertRaisesRegex(source_index.SourceIndexError, "candidate"):
            source_index.open_repository_index(candidate)

    def test_candidate_refuses_a_mutation_during_actual_git_hash_proof(self) -> None:
        target = self.code / "pkg/source.py"
        original_bytes = target.read_bytes()
        expected_blob = self._git("rev-parse", f"{self.tree}:pkg/source.py")
        with source_index.open_repository_index(self._candidate_trees()) as index:
            ready = index.paths.readiness
            prior = ready.read_bytes()
        original = source_index_candidate.run_git
        for operation, reason in (
            ("index", "candidate bytes differ"),
            ("resolve", "candidate changed during proof"),
        ):
            with self.subTest(operation=operation):
                target.write_bytes(original_bytes)
                changed = False

                def mutate_after_hash(
                    root: Path, args: list[str]
                ) -> subprocess.CompletedProcess[str]:
                    nonlocal changed
                    result = original(root, args)
                    if args[0] == "hash-object" and "pkg/source.py" in args and not changed:
                        self.assertEqual(result.returncode, 0)
                        position = args[3:].index("pkg/source.py")
                        self.assertEqual(result.stdout.splitlines()[position], expected_blob)
                        target.write_text("def candidate_symbol():\n    return 2\n")
                        changed = True
                    return result

                with (
                    mock.patch.object(source_index_candidate, "run_git", new=mutate_after_hash),
                    self.assertRaisesRegex(source_index.SourceIndexError, reason),
                ):
                    candidate = self._candidate_trees()
                    if operation == "index":
                        # Cache validation catches the first post-hash identity refusal;
                        # rebuilding must then reject the actual dirty bytes as well.
                        source_index.open_repository_index(candidate)
                    else:
                        candidate.resolve("pkg/source.py")
                self.assertTrue(changed)
                self.assertEqual(ready.read_bytes(), prior)

    def test_candidate_ignores_actual_gitlink_entries(self) -> None:
        commit = self._git("rev-parse", "HEAD")
        self._git("update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/submodule")
        candidate = self._candidate_trees(self._git("write-tree"))
        selected = candidate.source_candidate
        assert selected is not None
        self.assertNotIn("vendor/submodule", selected.members)
        self.assertIsNone(candidate.resolve("vendor/submodule"))
        with source_index.open_repository_index(candidate) as index:
            self.assertEqual(index.files_indexed, 5)

    def test_candidate_refuses_unsafe_git_census_paths(self) -> None:
        original = source_index_candidate.run_git
        for path in ("", "/outside.py", "../outside.py", "pkg//source.py", "duplicate"):
            with self.subTest(path=path):
                altered = False

                def malformed_listing(
                    root: Path, args: list[str], malformed_path: str = path
                ) -> subprocess.CompletedProcess[str]:
                    nonlocal altered
                    result = original(root, args)
                    if args[0] != "ls-tree":
                        return result
                    self.assertEqual(result.returncode, 0)
                    first = result.stdout.split("\0", 1)[0]
                    header = first.split("\t", 1)[0]
                    # Canonical Git cannot emit these traversal/duplicate names. Keep
                    # its real object metadata and inject only the malformed transport row.
                    row = first if malformed_path == "duplicate" else f"{header}\t{malformed_path}"
                    altered = True
                    return subprocess.CompletedProcess(
                        result.args, result.returncode, result.stdout + row + "\0", result.stderr
                    )

                with (
                    mock.patch.object(source_index_candidate, "run_git", new=malformed_listing),
                    self.assertRaisesRegex(source_index.SourceIndexError, "unsafe member"),
                ):
                    self._candidate_trees().resolve("pkg/source.py")
                self.assertTrue(altered)

    def test_candidate_refuses_a_symlink_root_before_member_hashing(self) -> None:
        alias = self.root / "code-alias"
        alias.symlink_to(self.code, target_is_directory=True)
        selected = source_index_candidate.GitSourceCandidate(alias, self.tree)
        with self.assertRaisesRegex(source_index.SourceIndexError, "root is not a real directory"):
            selected.resolve("pkg/source.py")

    def test_candidate_refuses_a_truncated_actual_hash_response_without_replacing_readiness(
        self,
    ) -> None:
        with source_index.open_repository_index(self._candidate_trees()) as index:
            ready = index.paths.readiness
            prior = ready.read_bytes()
        original = source_index_candidate.run_git
        hashed = False

        def truncated_hash(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal hashed
            result = original(root, args)
            if args[0] != "hash-object":
                return result
            self.assertEqual(result.returncode, 0)
            self.assertEqual(len(result.stdout.splitlines()), len(args[3:]))
            hashed = True
            return subprocess.CompletedProcess(result.args, result.returncode, "", result.stderr)

        with (
            mock.patch.object(source_index_candidate, "run_git", new=truncated_hash),
            self.assertRaisesRegex(source_index.SourceIndexError, "hash population differs"),
        ):
            source_index.open_repository_index(self._candidate_trees())
        self.assertTrue(hashed)
        self.assertEqual(ready.read_bytes(), prior)

    def test_candidate_resolves_a_memory_only_file_without_admitting_code_competitors(self) -> None:
        memory_file = self.memory / "memory-note.md"
        memory_file.write_text("Memory-owned evidence.\n")
        memory_escape = self.memory / "escaped.py"
        memory_escape.symlink_to(self.code / "pkg/source.py")
        self.write("new.py", "untracked_competitor = 1\n")
        candidate = self._candidate_trees()
        self.assertEqual(candidate.resolve("memory-note.md"), memory_file)
        self.assertEqual(candidate.resolve("pkg/source.py"), self.code / "pkg/source.py")
        self.assertIsNone(candidate.resolve("new.py"))
        self.assertIsNone(candidate.resolve("escaped.py"))

    def test_candidate_proves_a_population_larger_than_one_hash_batch(self) -> None:
        for number in range(70):
            self.write(f"many/source_{number:02d}.py", f"owned_symbol_{number} = {number}\n")
        self._git("add", "many")
        candidate = self._candidate_trees(self._git("write-tree"))
        with source_index.open_repository_index(candidate) as index:
            for number in (0, 69):
                anchor = model.Anchor(model.SYMBOL, f"owned_symbol_{number}")
                locations = symbol_index.locate((anchor,), candidate, index=index)[anchor].locations
                self.assertEqual([one.path for one in locations], [f"many/source_{number:02d}.py"])
        self.write("many/source_69.py", "owned_symbol_69 = 0\n")
        with self.assertRaisesRegex(source_index.SourceIndexError, "candidate"):
            source_index.open_repository_index(candidate)

    def test_candidate_size_cap_refuses_before_hashing_tracked_or_dirty_oversized_members(
        self,
    ) -> None:
        target = self.code / "pkg/source.py"
        original_bytes = target.read_bytes()
        with source_index.open_repository_index(self._candidate_trees()) as index:
            ready = index.paths.readiness
            original_ready = ready.read_bytes()
        original_git = source_index_candidate.run_git

        def refuse_hash(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[0] == "hash-object":
                raise AssertionError("oversized candidate started unbounded Git hashing")
            return original_git(root, args)

        oversized = b"x" * (source_index_state.MAX_SOURCE_FILE_BYTES + 1)
        for kind in ("tracked-oversized", "dirty-oversized"):
            with self.subTest(kind=kind):
                target.write_bytes(oversized)
                tree = self.tree
                if kind == "tracked-oversized":
                    self._git("add", "pkg/source.py")
                    tree = self._git("write-tree")
                try:
                    with mock.patch.object(source_index_candidate, "run_git", new=refuse_hash):
                        with self.assertRaisesRegex(source_index.SourceIndexError, "per-file"):
                            source_index.open_repository_index(self._candidate_trees(tree))
                        self.assertEqual(ready.read_bytes(), original_ready)
                        with self.assertRaisesRegex(source_index.SourceIndexError, "per-file"):
                            self._candidate_trees(tree).resolve("pkg/source.py")
                        self.assertEqual(ready.read_bytes(), original_ready)
                finally:
                    target.write_bytes(original_bytes)
                    self._git("add", "pkg/source.py")
        self.assertEqual(self._git("write-tree"), self.tree)

    def test_candidate_population_caps_refuse_before_hashing_and_preserve_prior_readiness(
        self,
    ) -> None:
        with source_index.open_repository_index(self._candidate_trees()) as index:
            ready = index.paths.readiness
            original_ready = ready.read_bytes()
            prior_count = index.files_indexed
            prior_bytes = index.source_bytes
        self.write("pkg/extra.py", "#" + " bounded fixture" * 100 + "\n")
        self._git("add", "pkg/extra.py")
        tree = self._git("write-tree")
        original_git = source_index_candidate.run_git

        def refuse_hash(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[0] == "hash-object":
                raise AssertionError("over-cap candidate started Git hashing")
            return original_git(root, args)

        limits = (("MAX_SOURCE_FILES", prior_count), ("MAX_SOURCE_BYTES", prior_bytes + 1))
        for name, maximum in limits:
            with (
                self.subTest(bound=name),
                mock.patch.object(source_index_state, name, maximum),
                mock.patch.object(source_index_candidate, "run_git", new=refuse_hash),
            ):
                with self.assertRaisesRegex(source_index.SourceIndexError, "cap"):
                    source_index.open_repository_index(self._candidate_trees(tree))
                self.assertEqual(ready.read_bytes(), original_ready)
