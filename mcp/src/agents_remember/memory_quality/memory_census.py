"""Deterministic governed-memory census from contract-owned exact Git scope."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agents_remember.kernel.coordination_context_resolver import (
    StorageSettings,
    is_sidecar_storage,
    mirror_onboarding_path,
    resolve_storage_for_source,
)
from agents_remember.kernel.git_command import run_git
from agents_remember.kernel.onboarding_doc import ROUTE_OVERVIEW_DOC_TYPES
from agents_remember.memory_quality.integrity.onboarding_drift_check.discovery import (
    normalize_overview_route,
    parse_table_metadata_text,
)
from agents_remember.memory_quality.integrity.onboarding_drift_check.entities import (
    parse_entity_census_rows,
)
from agents_remember.memory_quality.integrity.onboarding_drift_check.inline import (
    extract_inline_onboarding_block,
)
from agents_remember.models.lifecycles.memory_census import (
    GovernedArtifactIdentity,
    MemoryCensusBlocker,
    MemoryCensusResult,
    MemoryCensusRow,
    require_git_relative_path,
)
from agents_remember.worktrees.integration.closeout.memory_census_scope import MemoryCensusScope


def _git(root: Path, arguments: list[str]) -> str:
    result = run_git(root, arguments)
    if result.returncode != 0:
        raise ValueError("cannot read exact census Git object")
    result.stdout.encode("utf-8", errors="strict")
    return result.stdout


@dataclass
class _Tree:
    root: Path
    tree: str
    members: dict[str, str] = field(init=False)
    texts: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    metadata_loaded: bool = False

    def __post_init__(self) -> None:
        self.members = {}
        for row in _git(self.root, ["ls-tree", "-r", "-z", self.tree]).split("\0"):
            if not row:
                continue
            header, path = row.split("\t", 1)
            require_git_relative_path(path)
            if path in self.members:
                raise ValueError("duplicate exact Git census path")
            self.members[path] = header.split()[0]

    def text(self, path: str) -> str:
        require_git_relative_path(path)
        if self.members.get(path) not in {"100644", "100755"}:
            raise ValueError(f"governed artifact is not a regular Git file: {path}")
        if path not in self.texts:
            self.texts[path] = _git(self.root, ["show", f"{self.tree}:{path}"])
        return self.texts[path]

    def load_metadata(self, prefix: str) -> None:
        """Read table metadata in one exact-tree Git query, then use its canonical parser."""
        if self.metadata_loaded:
            return
        result = run_git(
            self.root,
            [
                "grep",
                "--no-color",
                "--no-line-number",
                "--no-column",
                "--no-heading",
                "-G",
                "-I",
                "-z",
                "-e",
                "^[[:space:]]*|",
                self.tree,
                "--",
                f"{prefix}/",
            ],
        )
        if result.returncode not in {0, 1}:
            raise ValueError("cannot read exact census document metadata")
        result.stdout.encode("utf-8", errors="strict")
        lines: dict[str, list[str]] = {}
        output = result.stdout
        cursor = 0
        tree_prefix = f"{self.tree}:"
        while cursor < len(output):
            name_end = output.find("\0", cursor)
            if name_end < 0 or not output.startswith(tree_prefix, cursor):
                raise ValueError("invalid exact-tree metadata record")
            line_end = output.find("\n", name_end + 1)
            if line_end < 0:
                raise ValueError("unterminated exact-tree metadata record")
            path = output[cursor + len(tree_prefix) : name_end]
            line = output[name_end + 1 : line_end]
            cursor = line_end + 1
            require_git_relative_path(path)
            if path.endswith(".md"):
                lines.setdefault(path, []).append(line)
        for path, mode in self.members.items():
            if not path.startswith(f"{prefix}/") or not path.endswith(".md"):
                continue
            if mode not in {"100644", "100755"}:
                continue
            self.metadata[path] = parse_table_metadata_text("\n".join(lines.get(path, ())))
        self.metadata_loaded = True

    def meta(self, path: str) -> dict[str, str]:
        if path not in self.metadata:
            self.metadata[path] = parse_table_metadata_text(self.text(path))
        return self.metadata[path]


@dataclass
class _Census:
    scope: MemoryCensusScope
    settings: StorageSettings
    before: _Tree
    after: _Tree
    onboarding: str
    rows: dict[tuple[bytes, bytes, int, bytes], MemoryCensusRow] = field(default_factory=dict)
    blockers: list[MemoryCensusBlocker] = field(default_factory=list)
    unonboarded: set[str] = field(default_factory=set)

    def block(
        self,
        code: str,
        detail: str,
        *,
        source: str | None = None,
        identity: GovernedArtifactIdentity | None = None,
    ) -> None:
        blocker = MemoryCensusBlocker(
            code=code, detail=detail, sourcePath=source, identity=identity
        )
        if blocker not in self.blockers:
            self.blockers.append(blocker)

    def add(
        self,
        identity: GovernedArtifactIdentity,
        *,
        source: str | None = None,
        reason: str,
        before: bool | None = None,
        present: bool | None = None,
    ) -> None:
        path = identity.memoryRootRelativePath
        root = self.after.root.resolve()
        if not (root / path).resolve().is_relative_to(root):
            self.block(
                "artifact-path-escape",
                "Governed artifact escapes the memory root.",
                source=source,
                identity=identity,
            )
            return
        if path in self.after.members and self.after.members[path] not in {"100644", "100755"}:
            self.block(
                "unsupported-artifact-file-type",
                "Governed artifact is not a regular file.",
                source=source,
                identity=identity,
            )
            return
        existed = path in self.before.members if before is None else before
        exists = path in self.after.members if present is None else present
        if not exists and not existed:
            self.block(
                "missing",
                "Required governed artifact has no final candidate mapping.",
                source=source,
                identity=identity,
            )
            return
        if not exists:
            self.block(
                "removal-disposition-required",
                "Physical absence requires the canonical curator removed disposition.",
                source=source,
                identity=identity,
            )
        old = self.rows.get(identity.sort_key)
        sources = set(old.sourcePaths if old else ())
        reasons = set(old.reasons if old else ())
        if source is not None:
            sources.add(source)
        reasons.add(reason)
        row = MemoryCensusRow(
            identity=identity,
            expectedFinalPresence="present" if exists else "absent",
            sourcePaths=tuple(sorted(sources, key=lambda value: value.encode("utf-8"))),
            reasons=tuple(sorted(reasons)),
            preExisting=existed,
        )
        if old and (old.expectedFinalPresence, old.preExisting) != (
            row.expectedFinalPresence,
            row.preExisting,
        ):
            self.block(
                "contradictory-artifact-presence",
                "Artifact presence evidence disagrees.",
                source=source,
                identity=identity,
            )
            return
        self.rows[identity.sort_key] = row

    def documents(self, tree: _Tree) -> dict[str, dict[str, str]]:
        tree.load_metadata(self.onboarding)
        return {
            path: tree.meta(path)
            for path, mode in tree.members.items()
            if path.startswith(f"{self.onboarding}/")
            and path.endswith(".md")
            and mode in {"100644", "100755"}
        }

    def sources(self, documents: tuple[dict[str, dict[str, str]], ...]) -> set[str]:
        working_new = {
            change.new_path
            for change in self.scope.working_changes
            if change.status in {"A", "R", "C"} and change.new_path is not None
        }
        sources = set(self.scope.working_paths) | set(self.scope.committed_paths)
        included = set()
        for source in sorted(sources, key=lambda value: value.encode("utf-8")):
            require_git_relative_path(source)
            mode = resolve_storage_for_source(
                source, self.settings, self.scope.pair_identity.repoId
            )
            if mode == "disabled":
                continue
            included.add(source)
            if is_sidecar_storage(mode):
                self.sidecars(source, documents, required=source in working_new)
            elif mode == "inline":
                self.inline(source, required=source in working_new)
            else:
                self.block(
                    "unsupported-storage",
                    f"Unsupported governed storage mode: {mode}",
                    source=source,
                )
        return included

    def sidecars(
        self, source: str, documents: tuple[dict[str, dict[str, str]], ...], *, required: bool
    ) -> None:
        expected = mirror_onboarding_path(Path(self.onboarding), source).as_posix()
        mapped = {
            path
            for document in documents
            for path, metadata in document.items()
            if metadata.get("doc_type") == "file-level-onboarding"
            and metadata.get("path") == source
        }
        if any(path != expected for path in mapped):
            self.block(
                "ambiguous-onboarding-mapping",
                "Source has a noncanonical or multiple declared sidecar mapping.",
                source=source,
            )
        if expected in self.after.members or expected in self.before.members:
            mapped.add(expected)
        if not mapped:
            if required:
                self.add(
                    GovernedArtifactIdentity(
                        artifactType="file-sidecar", memoryRootRelativePath=expected
                    ),
                    source=source,
                    reason="current-task-addition",
                )
            else:
                self.unonboarded.add(source)
        for path in sorted(mapped):
            for document in documents:
                metadata = document.get(path)
                if metadata is not None and (
                    metadata.get("doc_type") != "file-level-onboarding"
                    or metadata.get("path") != source
                ):
                    self.block(
                        "contradictory-onboarding-mapping",
                        "Canonical sidecar declares a different source or artifact type.",
                        source=source,
                    )
            self.add(
                GovernedArtifactIdentity(artifactType="file-sidecar", memoryRootRelativePath=path),
                source=source,
                reason="changed-source",
            )

    def inline(self, source: str, *, required: bool) -> None:
        code_root = Path(self.scope.pair_identity.codeRoot)
        try:
            path = (code_root / source).relative_to(self.after.root).as_posix()
        except ValueError:
            self.block(
                "unsupported-inline-memory-root",
                "Inline artifact cannot be identified relative to the accepted memory root.",
                source=source,
            )
            return
        before = (
            path in self.before.members
            and extract_inline_onboarding_block(self.before.text(path)) is not None
        )
        present = (
            path in self.after.members
            and extract_inline_onboarding_block(self.after.text(path)) is not None
        )
        if before or present or required:
            self.add(
                GovernedArtifactIdentity(
                    artifactType="inline-onboarding", memoryRootRelativePath=path
                ),
                source=source,
                reason="changed-source",
                before=before,
                present=present,
            )
        else:
            self.unonboarded.add(source)

    def overviews(
        self, sources: set[str], documents: tuple[dict[str, dict[str, str]], ...]
    ) -> None:
        for source in sorted(sources):
            for document in documents:
                matches = []
                for path, metadata in document.items():
                    if metadata.get("doc_type") not in ROUTE_OVERVIEW_DOC_TYPES:
                        continue
                    raw_route = metadata.get("sourceRoute", ".")
                    try:
                        route = normalize_overview_route(raw_route)
                    except (UnicodeError, ValueError) as error:
                        self.block(
                            "invalid-governing-route",
                            f"{path}: {error}",
                            source=source,
                            identity=GovernedArtifactIdentity(
                                artifactType="route-overview",
                                memoryRootRelativePath=path,
                            ),
                        )
                        continue
                    if route in {".", source} or source.startswith(f"{route}/"):
                        matches.append((0 if route == "." else len(route.split("/")), path))
                if matches:
                    nearest = max(depth for depth, _ in matches)
                    selected = [path for depth, path in matches if depth == nearest]
                    if len(selected) != 1:
                        self.block(
                            "ambiguous-governing-overview",
                            "Multiple nearest overviews govern the same source.",
                            source=source,
                        )
                    for path in selected:
                        self.add(
                            GovernedArtifactIdentity(
                                artifactType="route-overview", memoryRootRelativePath=path
                            ),
                            source=source,
                            reason="nearest-governing-route",
                        )

    def edited_documents(self, documents: tuple[dict[str, dict[str, str]], ...]) -> None:
        for path in self.scope.memory_paths:
            types = {document[path].get("doc_type") for document in documents if path in document}
            if len(types) > 1:
                self.block(
                    "contradictory-artifact-type",
                    f"Task-edited artifact changes governed type: {path}",
                )
            for document in documents:
                metadata = document.get(path)
                if metadata is None:
                    continue
                kind = metadata.get("doc_type")
                if kind in ROUTE_OVERVIEW_DOC_TYPES:
                    self.add(
                        GovernedArtifactIdentity(
                            artifactType="route-overview", memoryRootRelativePath=path
                        ),
                        reason="task-edited-overview",
                    )
                elif kind == "file-level-onboarding":
                    source = metadata.get("path", "")
                    require_git_relative_path(source)
                    expected = mirror_onboarding_path(Path(self.onboarding), source).as_posix()
                    if path != expected:
                        self.block(
                            "ambiguous-onboarding-mapping",
                            "Task-edited sidecar is not its source's canonical mapping.",
                            source=source,
                        )
                    self.add(
                        GovernedArtifactIdentity(
                            artifactType="file-sidecar", memoryRootRelativePath=path
                        ),
                        source=source,
                        reason="task-edited-onboarding",
                    )

    def entities(self, sources: set[str], documents: tuple[dict[str, dict[str, str]], ...]) -> None:
        catalogs = {
            path
            for document in documents
            for path, metadata in document.items()
            if metadata.get("doc_type") == "repo-entity-catalog" or path.endswith("/entities.md")
        }
        for path in sorted(catalogs):
            try:
                before = (
                    {row.key: row for row in parse_entity_census_rows(self.before.text(path))}
                    if path in self.before.members
                    else {}
                )
                after = (
                    {row.key: row for row in parse_entity_census_rows(self.after.text(path))}
                    if path in self.after.members
                    else {}
                )
                for row in (*before.values(), *after.values()):
                    for evidence in row.evidence_paths:
                        require_git_relative_path(evidence)
            except ValueError as error:
                self.block("ambiguous-entity-catalog", f"{path}: {error}")
                continue
            for key in sorted(set(before) | set(after)):
                evidence = set()
                for row in (before.get(key), after.get(key)):
                    if row is not None:
                        evidence.update(row.evidence_paths)
                affected = {
                    source
                    for source in sources
                    for item in evidence
                    if source == item
                    or source.startswith(f"{item}/")
                    or item.startswith(f"{source}/")
                }
                edited = path in self.scope.memory_paths and before.get(key) != after.get(key)
                if affected or edited:
                    for source in sorted(affected) or [None]:
                        self.add(
                            GovernedArtifactIdentity(
                                artifactType="entity-row",
                                memoryRootRelativePath=path,
                                subIdentity=key,
                            ),
                            source=source,
                            reason="task-edited-entity"
                            if edited
                            else "entity-evidence-intersection",
                            before=key in before,
                            present=key in after,
                        )


def build_memory_census(
    scope: MemoryCensusScope, *, settings: StorageSettings
) -> MemoryCensusResult:
    """Map the complete captured scope; this grants no semantic acceptance authority."""
    pair = scope.pair_identity
    memory_root = Path(pair.memoryRoot)
    onboarding = Path(pair.onboardingRoot).relative_to(memory_root).as_posix()
    require_git_relative_path(onboarding)
    before = _Tree(memory_root, scope.memory_baseline_commit)
    after = _Tree(memory_root, scope.memory_candidate_tree)
    census = _Census(scope, settings, before, after, onboarding)
    try:
        documents = (census.documents(before), census.documents(after))
        sources = census.sources(documents)
        census.overviews(sources, documents)
        census.edited_documents(documents)
        census.entities(sources, documents)
    except (OSError, UnicodeError, ValueError) as error:
        census.block("unresolvable-census", str(error))
    return MemoryCensusResult(
        rows=tuple(census.rows[key] for key in sorted(census.rows)),
        blockers=tuple(
            sorted(
                census.blockers,
                key=lambda row: (
                    row.code,
                    row.sourcePath or "",
                    row.identity.sort_key if row.identity else (),
                    row.detail,
                ),
            )
        ),
        unonboarded=tuple(sorted(census.unonboarded, key=lambda value: value.encode("utf-8"))),
    )


__all__ = ["build_memory_census"]
