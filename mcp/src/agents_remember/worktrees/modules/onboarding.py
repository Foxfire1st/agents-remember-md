from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal

from agents_remember.kernel import coordination_context_resolver as resolver
from agents_remember.kernel import filesystem
from agents_remember.kernel.onboarding_doc import (
    ROUTE_OVERVIEW_DOC_TYPES,
    has_no_impact_marker,
    markdown_table_cells,
    meaningful_body,
    meaningful_body_changed,
    new_history_lines,
    normalize_route,
    onboarding_metadata_row,
    table_metadata,
)
from agents_remember.kernel.route_index import build_route_indexes
from agents_remember.worktrees.modules.context import contract_context
from agents_remember.worktrees.modules.git import (
    changed_worktree_paths,
    commit_text_or_none,
    committed_changed_paths,
    require_git,
    run_git,
)
from agents_remember.worktrees.modules.models import (
    PATH_SAMPLE_LIMIT,
    EntityFingerprintRefreshPlan,
    EntityFingerprintRequiredItem,
    EntityFingerprintRow,
    OnboardingRefreshPlan,
    RouteOverviewBodyClassification,
    RouteOverviewRefreshPlan,
    SidecarBodyClassification,
    VerifiedChange,
)
from agents_remember.worktrees.modules.onboarding_acceptance import (
    OnboardingBodyGateEvidence,
    apply_route_no_impact,
    apply_sidecar_no_impact,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract

ENTITY_FINGERPRINT_ALGORITHM = "git-blob-set-v1"


def sidecar_onboarding_path(onboarding_root: Path, source_path: str) -> Path:
    return onboarding_root / f"{source_path}.md"


def contract_memory_verified_commit(contract: WorktreeContract) -> str:
    """The last memory commit closeout verified against, for body-gate baselines."""
    return contract.ledger_commit or contract.memory_content_commit or contract.memory_base_commit


def _changed_memory_paths(memory_root: Path, memory_verified_commit: str) -> set[str]:
    """Memory-tree paths changed since the last verified memory commit.

    Dirty paths plus the committed range, so sidecar work committed in the
    memory worktree before closeout still counts as updated this task.
    """
    changed = set(changed_worktree_paths(memory_root))
    if memory_verified_commit:
        changed.update(
            committed_changed_paths(memory_root, memory_verified_commit, memory_verified_commit)
        )
    return changed


def _joined_sample(paths: list[str]) -> str:
    """Comma-join capped at PATH_SAMPLE_LIMIT so gate errors never flood payloads."""
    extra = len(paths) - PATH_SAMPLE_LIMIT
    joined = ", ".join(paths[:PATH_SAMPLE_LIMIT])
    return f"{joined}, ... (+{extra} more)" if extra > 0 else joined


def onboarding_refresh_plan_for_context(
    context, changed_paths: list[str], *, working_paths: list[str] | None = None
) -> OnboardingRefreshPlan:
    """Plan sidecar refreshes for changed sources, split by responsibility tier.

    ``working_paths`` names the paths changed in the working tree; paths outside
    it arrived via commits (merges, pre-committed slices). Working paths without
    onboarding block closeout (``missing``/``unsupported``); committed-range
    paths without onboarding are collected as ``unonboarded`` and never block,
    so transported history cannot force whole-repository onboarding. ``None``
    keeps the strict legacy semantics: every changed path is treated as working.
    """
    working = set(changed_paths if working_paths is None else working_paths)
    required: list[dict[str, str]] = []
    missing: list[str] = []
    unsupported: list[str] = []
    unonboarded: list[str] = []
    for source_path in changed_paths:
        storage = resolver.resolve_storage_for_source(
            source_path, context.storage, context.code_repository_name
        )
        if storage == "disabled":
            continue
        if not resolver.is_sidecar_storage(storage):
            (unsupported if source_path in working else unonboarded).append(source_path)
            continue
        onboarding_path = sidecar_onboarding_path(context.onboarding_root, source_path)
        if not filesystem.exists(onboarding_path):
            (missing if source_path in working else unonboarded).append(source_path)
            continue
        required.append(
            {
                "source_path": source_path,
                "onboarding_file": onboarding_path.as_posix(),
            }
        )
    return {
        "required": required,
        "missing": missing,
        "unsupported": unsupported,
        "unonboarded": unonboarded,
    }


def normalized_table_cell(cell: str) -> str:
    return re.sub(r"[^a-z0-9]", "", cell.lower())


def route_overview_metadata_refresh_plan_for_context(
    context,
    changed_paths: list[str],
    *,
    memory_tree: Path | None = None,
    memory_verified_commit: str = "",
) -> RouteOverviewRefreshPlan:
    """Plan verification stamps for nearest governing and task-edited overviews.

    A curator can repair a route whose source drift predates the leaf's current code
    range (for example after a source-branch sync).  Such an overview is still part of
    this closeout transaction even though none of ``changed_paths`` falls below its
    route.  Include every route overview changed since the task's verified memory
    baseline so the body gate can validate it and closeout can stamp it atomically.
    Untouched ancestors outside that closed set must not receive verification stamps.
    """
    required: list[dict[str, str]] = []
    missing_metadata: list[str] = []
    tree = Path(memory_tree).resolve() if memory_tree is not None else None
    changed_memory = (
        _changed_memory_paths(tree, memory_verified_commit) if tree is not None else set()
    )
    overviews = []
    for overview_path in sorted(context.onboarding_root.rglob("overview.md")):
        if not filesystem.is_file(overview_path):
            continue
        metadata = table_metadata(overview_path)
        if metadata.get("doc_type", "").strip("`") not in ROUTE_OVERVIEW_DOC_TYPES:
            continue
        source_route = normalize_route(metadata.get("sourceRoute", "."))
        overviews.append((overview_path, metadata, source_route))
    routes = [source_route for _, _, source_route in overviews]
    governing_routes = {
        route
        for path in changed_paths
        if (route := _nearest_governing_route(path, routes)) is not None
    }
    for overview_path, metadata, source_route in overviews:
        changed_in_memory = False
        if tree is not None:
            try:
                changed_in_memory = (
                    overview_path.resolve().relative_to(tree).as_posix() in changed_memory
                )
            except ValueError:
                changed_in_memory = False
        if source_route not in governing_routes and not changed_in_memory:
            continue
        rel_path = overview_path.relative_to(context.onboarding_root).as_posix()
        if "lastVerifiedCommitHash" not in metadata or "lastVerifiedCommitDate" not in metadata:
            missing_metadata.append(rel_path)
            continue
        required.append(
            {
                "source_route": source_route,
                "onboarding_file": overview_path.as_posix(),
            }
        )
    return {
        "required": required,
        "missing_metadata": missing_metadata,
    }


def _nearest_governing_route(source_path: str, routes: list[str]) -> str | None:
    """The longest route among routes that covers source_path; '.' covers all."""
    candidates = [
        route
        for route in routes
        if route in {".", source_path} or source_path.startswith(f"{route}/")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda route: (route != ".", len(route), route))


def classify_route_overview_updates(
    context,
    plan: RouteOverviewRefreshPlan,
    changed_paths: list[str],
    *,
    memory_tree: Path | None = None,
    memory_verified_commit: str = "",
) -> RouteOverviewBodyClassification:
    """Classify matched route overviews by meaningful body and history changes.

    Only overviews that are the **nearest governor** of at least one changed
    source path are domain-evident and classified like sidecars (``stale`` /
    ``untraced`` / ``attested_no_impact``, with ``No route impact:`` as the
    marker). Overviews matched only as ancestors — including the repo-root
    overview matched by happenstance — are reported under
    ``stamped_without_body_review`` when their body was not reviewed this
    cycle, and never gate closeout.
    """
    classification: RouteOverviewBodyClassification = {
        "stale": [],
        "untraced": [],
        "attested_no_impact": [],
        "stamped_without_body_review": [],
    }
    required = plan["required"]
    if not required:
        return classification
    tree = memory_tree if memory_tree is not None else getattr(context, "memory_root", None)
    if tree is None:
        return classification
    memory_root = Path(tree).resolve()
    baseline_ref = memory_verified_commit or "HEAD"
    changed_memory = _changed_memory_paths(memory_root, memory_verified_commit)
    routes = [normalize_route(item["source_route"]) for item in required]
    domain_routes = {
        route
        for route in (_nearest_governing_route(path, routes) for path in changed_paths)
        if route is not None
    }
    changed_overviews: set[Path] = set()
    for item in required:
        overview_path = Path(item["onboarding_file"]).resolve()
        try:
            relative = overview_path.relative_to(memory_root).as_posix()
        except ValueError:
            continue
        if relative in changed_memory:
            changed_overviews.add(overview_path)
    for item in required:
        route = normalize_route(item["source_route"])
        overview_path = Path(item["onboarding_file"]).resolve()
        source_domain_evident = route in domain_routes
        evidence: Literal["ancestor", "source", "task-edited"] = (
            "source"
            if source_domain_evident
            else "task-edited"
            if overview_path in changed_overviews
            else "ancestor"
        )
        bucket = _route_overview_bucket(
            overview_path,
            memory_root=memory_root,
            baseline_ref=baseline_ref,
            changed_memory=changed_memory,
            evidence=evidence,
        )
        if bucket is not None:
            classification[bucket].append(route)
    return classification


def _overview_revision(
    overview_path: Path,
    *,
    memory_root: Path,
    baseline_ref: str,
    changed_memory: set[str],
) -> tuple[bool, list[str], bool] | None:
    """Body change, added history, and citation-only status against the baseline.

    ``None`` when the overview is outside the memory tree or absent from the baseline —
    neither is a stale-overview signal, so those overviews drop out of the classification.
    """
    try:
        relative = overview_path.relative_to(memory_root).as_posix()
    except ValueError:
        return None
    baseline_text = commit_text_or_none(memory_root, baseline_ref, relative)
    if baseline_text is None:
        return None
    current = filesystem.read_text(overview_path, encoding="utf-8")
    body_changed = relative in changed_memory and meaningful_body_changed(baseline_text, current)
    return (
        body_changed,
        new_history_lines(baseline_text, current),
        body_changed and _citation_coordinates_only_changed(baseline_text, current),
    )


def _citation_coordinates_only_changed(old_text: str, new_text: str) -> bool:
    """True when meaningful route prose is unchanged apart from citation line ranges.

    The sanctioned citation fixer moves only the final source-range cell of reference
    table rows.  Those generated coordinates do not need an Update History entry, but
    they do need the document's verification stamp to advance with the code snapshot.
    Normalising only complete ``path:line[-line]`` cells keeps substantive prose, claim,
    anchor, path, and table-shape changes visible to the body gate.
    """

    def normalized(text: str) -> str:
        lines: list[str] = []
        for line in meaningful_body(text).splitlines():
            cells = markdown_table_cells(line) if line.strip().startswith("|") else []
            if not cells:
                lines.append(line)
                continue
            references: list[str] = []
            for reference in cells[-1].split(";"):
                value = reference.strip()
                matched = re.fullmatch(r"(.+):\d+(?:-\d+)?", value)
                prefix = matched.group(1) if matched is not None else ""
                references.append(
                    f"{prefix}:<lines>"
                    if matched is not None and ("/" in prefix or "." in prefix)
                    else value
                )
            lines.append("\x1f".join([*cells[:-1], "; ".join(references)]))
        return "\n".join(lines)

    return normalized(old_text) == normalized(new_text)


def _governing_overview_bucket(
    body_changed: bool,
    added_history: list[str],
    *,
    allow_citation_only: bool,
    citation_only: bool,
) -> str | None:
    """The gating bucket for a nearest-governor overview; ``None`` once it is properly updated."""
    if body_changed and added_history:
        return None
    if body_changed:
        if allow_citation_only and citation_only:
            return None
        return "untraced"
    if added_history and has_no_impact_marker(added_history):
        return "attested_no_impact"
    return "stale"


def _route_overview_bucket(
    overview_path: Path,
    *,
    memory_root: Path,
    baseline_ref: str,
    changed_memory: set[str],
    evidence: Literal["ancestor", "source", "task-edited"],
) -> str | None:
    """Which classification bucket one matched route overview falls in, if any.

    A source-matched or task-edited overview is classified like a sidecar; an
    overview matched merely as an ancestor is reported when its body went unreviewed
    and never gates. Task-edited citation-coordinate regeneration is accepted without
    an invented history entry; substantive task edits are not.
    """
    revision = _overview_revision(
        overview_path,
        memory_root=memory_root,
        baseline_ref=baseline_ref,
        changed_memory=changed_memory,
    )
    if revision is None:
        return None
    body_changed, added_history, citation_only = revision
    if evidence == "ancestor":
        return None if body_changed else "stamped_without_body_review"
    return _governing_overview_bucket(
        body_changed,
        added_history,
        allow_citation_only=evidence == "task-edited",
        citation_only=citation_only,
    )


def require_updated_route_overview_content(
    context,
    plan: RouteOverviewRefreshPlan,
    changed_paths: list[str],
    *,
    body_gate: OnboardingBodyGateEvidence | None = None,
) -> list[str]:
    """Fail closeout when a domain-evident route overview lacks an honest update.

    A route overview that directly governs a changed source path is the front
    door for that change: stamping its verification header over an untouched
    body silently presents stale route documentation as current. The overview
    must therefore pair a meaningful body change with a new Update History
    entry, or have an exact candidate-bound no-route-impact decision. Returns
    the accepted routes so closeout payloads can surface them.
    """
    evidence = body_gate or OnboardingBodyGateEvidence()
    classification = classify_route_overview_updates(
        context,
        plan,
        changed_paths,
        memory_tree=evidence.memory_tree,
        memory_verified_commit=evidence.memory_verified_commit,
    )
    classification = apply_route_no_impact(classification, evidence.accepted_no_impact)
    stale = classification["stale"]
    untraced = classification["untraced"]
    if stale or untraced:
        details: list[str] = []
        if stale:
            details.append(
                "the following governing route overviews have an unmodified or "
                "metadata/history-only body: " + ", ".join(sorted(stale))
            )
        if untraced:
            details.append(
                "the following governing route overviews have a body update without a new "
                "Update History entry: " + ", ".join(sorted(untraced))
            )
        raise RuntimeError(
            "external-memory closeout requires updated route overview content for routes "
            "whose governed sources changed; "
            + "; ".join(details)
            + ". Update each overview body through the c-05-create-or-update-onboarding-files "
            "skill and record the change in its Update History, or publish an exact "
            "candidate-bound no-route-impact judgment through curator_coherence. Advancing "
            "lastVerifiedCommitHash on stale content is a prohibited metadata-only refresh."
        )
    return classification["attested_no_impact"]


def validate_route_overview_refresh_plan_for_context(
    context,
    changed_paths: list[str],
    *,
    memory_tree: Path | None = None,
    memory_verified_commit: str = "",
    accepted_no_impact: frozenset[str] = frozenset(),
) -> RouteOverviewRefreshPlan:
    plan = route_overview_metadata_refresh_plan_for_context(
        context,
        changed_paths,
        memory_tree=memory_tree,
        memory_verified_commit=memory_verified_commit,
    )
    missing_metadata = plan["missing_metadata"]
    if missing_metadata:
        raise RuntimeError(
            "external-memory closeout requires route overview verification metadata before memory commit; "
            f"missing lastVerifiedCommitHash or lastVerifiedCommitDate in: {', '.join(missing_metadata)}. "
            "Run the c-05-create-or-update-onboarding-files skill, then rerun closeout."
        )
    require_updated_route_overview_content(
        context,
        plan,
        changed_paths,
        body_gate=OnboardingBodyGateEvidence(
            memory_tree=memory_tree,
            memory_verified_commit=memory_verified_commit,
            accepted_no_impact=accepted_no_impact,
        ),
    )
    return plan


def refresh_route_overview_metadata_for_context(
    context,
    change: VerifiedChange,
    *,
    memory_tree: Path | None = None,
    memory_verified_commit: str = "",
    accepted_no_impact: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    verified_commit = change.commit
    verified_date = change.commit_date
    plan = validate_route_overview_refresh_plan_for_context(
        context,
        change.changed_paths,
        memory_tree=memory_tree,
        memory_verified_commit=memory_verified_commit,
        accepted_no_impact=accepted_no_impact,
    )
    refreshed: list[dict[str, str]] = []
    for item in plan["required"]:
        overview_path = Path(item["onboarding_file"])
        text = filesystem.read_text(overview_path, encoding="utf-8")
        text, hash_found = onboarding_metadata_row(
            text, "lastVerifiedCommitHash", verified_commit, code=True
        )
        text, date_found = onboarding_metadata_row(text, "lastVerifiedCommitDate", verified_date)
        if not hash_found or not date_found:
            raise RuntimeError(
                "external-memory closeout requires route overview verification metadata before memory commit; "
                f"{overview_path.as_posix()} is missing lastVerifiedCommitHash or lastVerifiedCommitDate. "
                "Run the c-05-create-or-update-onboarding-files skill, then rerun closeout."
            )
        filesystem.write_text(overview_path, text, encoding="utf-8")
        refreshed.append(item)
    return refreshed


def refresh_route_indexes_for_context(context) -> dict[str, Any]:
    result = build_route_indexes(
        code_root=context.code_repository_root,
        onboarding_root=context.onboarding_root,
        repository=context.code_repository_name,
        storage=context.storage,
        dry_run=False,
    )
    return result.to_dict()


def route_index_refresh_plan_for_context(context) -> dict[str, Any]:
    result = build_route_indexes(
        code_root=context.code_repository_root,
        onboarding_root=context.onboarding_root,
        repository=context.code_repository_name,
        storage=context.storage,
        dry_run=True,
    )
    return result.to_dict()


def _fingerprint_table_header(lines: list[str]) -> tuple[dict[str, int], int] | None:
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = markdown_table_cells(line)
        normalized = {normalized_table_cell(cell): position for position, cell in enumerate(cells)}
        required = {"entity", "algorithm", "fingerprint", "evidencepaths"}
        if required.issubset(normalized):
            return {key: normalized[key] for key in required}, index + 2
    return None


def _fingerprint_row(line: str, index: int, header: dict[str, int]) -> EntityFingerprintRow | None:
    if not line.lstrip().startswith("|"):
        return None
    cells = markdown_table_cells(line)
    if len(cells) <= max(header.values()):
        return None
    algorithm = cells[header["algorithm"]].strip("`")
    evidence_cell = cells[header["evidencepaths"]]
    return {
        "line_index": index,
        "entity": cells[header["entity"]].strip("`"),
        "algorithm": algorithm,
        "fingerprint": cells[header["fingerprint"]].strip("`"),
        "evidence_paths": re.findall(r"`([^`]+)`", evidence_cell),
    }


def parse_entity_fingerprint_rows(catalog_path: Path) -> list[EntityFingerprintRow]:
    if not filesystem.exists(catalog_path):
        return []
    lines = filesystem.read_text(catalog_path, encoding="utf-8").splitlines()
    table = _fingerprint_table_header(lines)
    if table is None:
        return []
    header, start_index = table

    rows: list[EntityFingerprintRow] = []
    for index in range(start_index, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            break
        row = _fingerprint_row(line, index, header)
        if row is not None:
            rows.append(row)
    return rows


def entity_fingerprint_refresh_plan_for_context(
    context, changed_paths: list[str]
) -> EntityFingerprintRefreshPlan:
    changed = set(changed_paths)
    catalog_path = context.onboarding_root / "entities.md"
    required: list[EntityFingerprintRequiredItem] = []
    unsupported: list[dict[str, str]] = []
    for row in parse_entity_fingerprint_rows(catalog_path):
        evidence_paths = list(row["evidence_paths"])
        affected_paths = sorted(changed.intersection(evidence_paths))
        if not affected_paths:
            continue
        entity = str(row["entity"])
        if row["algorithm"] != ENTITY_FINGERPRINT_ALGORITHM:
            unsupported.append(
                {
                    "entity": entity,
                    "algorithm": str(row["algorithm"]),
                }
            )
            continue
        required.append(
            {
                "entity": entity,
                "onboarding_file": catalog_path.as_posix(),
                "evidence_paths": evidence_paths,
                "affected_paths": affected_paths,
            }
        )
    return {
        "required": required,
        "unsupported": unsupported,
    }


def compute_git_blob_set_fingerprint(repo_root: Path, evidence_paths: list[str]) -> str:
    lines: list[str] = []
    for source_path in sorted(evidence_paths):
        blob_hash = require_git(repo_root, ["rev-parse", f"HEAD:{source_path}"])
        lines.append(f"{source_path}\0{blob_hash}\n")
    digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def refresh_entity_fingerprints_for_context(
    context, changed_paths: list[str]
) -> list[dict[str, object]]:
    plan = entity_fingerprint_refresh_plan_for_context(context, changed_paths)
    unsupported = plan["unsupported"]
    if unsupported:
        details = ", ".join(f"{item['entity']} ({item['algorithm']})" for item in unsupported)
        raise RuntimeError(
            "external-memory closeout requires supported entity fingerprint rows before memory commit; "
            f"unsupported rows: {details}. Run the c-05-create-or-update-onboarding-files skill, then rerun closeout."
        )
    required = list(plan["required"])
    if not required:
        return []

    catalog_path = Path(required[0]["onboarding_file"])
    lines = filesystem.read_text(catalog_path, encoding="utf-8").splitlines()
    refreshed: list[dict[str, object]] = []
    rows_by_entity = {
        str(row["entity"]): row for row in parse_entity_fingerprint_rows(catalog_path)
    }
    for item in required:
        entity = str(item["entity"])
        row = rows_by_entity[entity]
        fingerprint = compute_git_blob_set_fingerprint(
            context.code_repository_root, list(item["evidence_paths"])
        )
        line_index = int(row["line_index"])
        old_fingerprint = str(row["fingerprint"])
        if old_fingerprint:
            lines[line_index] = lines[line_index].replace(old_fingerprint, fingerprint, 1)
        else:
            raise RuntimeError(
                "external-memory closeout requires entity fingerprint values before memory commit; "
                f"{catalog_path.as_posix()} row {entity!r} is missing a fingerprint. "
                "Run the c-05-create-or-update-onboarding-files skill, then rerun closeout."
            )
        refreshed.append(
            {
                "entity": entity,
                "onboarding_file": catalog_path.as_posix(),
                "fingerprint": fingerprint,
                "affected_paths": item["affected_paths"],
            }
        )
    filesystem.write_text(catalog_path, "\n".join(lines) + "\n", encoding="utf-8")
    return refreshed


def onboarding_refresh_plan(
    contract: WorktreeContract,
    changed_paths: list[str],
    *,
    working_paths: list[str] | None = None,
) -> OnboardingRefreshPlan:
    return onboarding_refresh_plan_for_context(
        contract_context(contract), changed_paths, working_paths=working_paths
    )


def entity_fingerprint_refresh_plan(
    contract: WorktreeContract, changed_paths: list[str]
) -> EntityFingerprintRefreshPlan:
    return entity_fingerprint_refresh_plan_for_context(contract_context(contract), changed_paths)


def route_overview_metadata_refresh_plan(
    contract: WorktreeContract, changed_paths: list[str]
) -> RouteOverviewRefreshPlan:
    return route_overview_metadata_refresh_plan_for_context(
        contract_context(contract),
        changed_paths,
        memory_tree=contract.memory_worktree,
        memory_verified_commit=contract_memory_verified_commit(contract),
    )


def validate_route_overview_refresh_plan(
    contract: WorktreeContract,
    changed_paths: list[str],
    *,
    accepted_no_impact: frozenset[str] = frozenset(),
) -> RouteOverviewRefreshPlan:
    return validate_route_overview_refresh_plan_for_context(
        contract_context(contract),
        changed_paths,
        memory_tree=contract.memory_worktree,
        memory_verified_commit=contract_memory_verified_commit(contract),
        accepted_no_impact=accepted_no_impact,
    )


def classify_sidecar_updates(
    context,
    plan: OnboardingRefreshPlan,
    *,
    memory_tree: Path | None = None,
    memory_verified_commit: str = "",
) -> SidecarBodyClassification:
    """Classify changed-source sidecars by meaningful body and history changes.

    ``stale`` collects unchanged sidecars, metadata-only edits, and history-only
    edits without the no-impact marker; ``untraced`` collects body edits that
    lack a new Update History entry; ``attested_no_impact`` collects
    history-only edits whose new entry carries the explicit
    ``No content impact:`` marker. New sidecars absent from the memory tree's
    HEAD pass without classification, and sidecars that do not resolve under
    the memory tree are skipped rather than reported as false findings.
    """
    classification: SidecarBodyClassification = {
        "stale": [],
        "untraced": [],
        "attested_no_impact": [],
    }
    required = plan["required"]
    if not required:
        return classification
    tree = memory_tree if memory_tree is not None else getattr(context, "memory_root", None)
    if tree is None:
        return classification
    memory_root = Path(tree).resolve()
    baseline_ref = memory_verified_commit or "HEAD"
    changed_memory = _changed_memory_paths(memory_root, memory_verified_commit)
    for item in required:
        onboarding_path = Path(item["onboarding_file"]).resolve()
        try:
            relative = onboarding_path.relative_to(memory_root).as_posix()
        except ValueError:
            continue
        if relative not in changed_memory:
            classification["stale"].append(item["source_path"])
            continue
        baseline_text = commit_text_or_none(memory_root, baseline_ref, relative)
        if baseline_text is None:
            continue
        current = filesystem.read_text(onboarding_path, encoding="utf-8")
        body_changed = meaningful_body_changed(baseline_text, current)
        added_history = new_history_lines(baseline_text, current)
        if body_changed and added_history:
            continue
        if body_changed:
            classification["untraced"].append(item["source_path"])
        elif added_history and has_no_impact_marker(added_history):
            classification["attested_no_impact"].append(item["source_path"])
        else:
            classification["stale"].append(item["source_path"])
    return classification


def require_updated_sidecar_content(
    context,
    plan: OnboardingRefreshPlan,
    *,
    memory_tree: Path | None = None,
    memory_verified_commit: str = "",
    accepted_no_impact: frozenset[str] = frozenset(),
) -> list[str]:
    """Fail closeout when a changed source file's sidecar lacks an honest update.

    Refreshing only verification metadata (or only the Update History) on an
    unchanged sidecar body silently defeats the commit-hash-based drift check,
    and a body edit without a new Update History entry loses traceability. A
    changed source file's sidecar must therefore pair a meaningful body change
    with a new history entry or have an exact candidate-bound no-content-impact
    decision. Returns accepted source paths so closeout payloads can surface
    them. The check is a no-op when no sidecar onboarding is required.
    """

    classification = classify_sidecar_updates(
        context, plan, memory_tree=memory_tree, memory_verified_commit=memory_verified_commit
    )
    classification = apply_sidecar_no_impact(classification, accepted_no_impact)
    stale = classification["stale"]
    untraced = classification["untraced"]
    if stale or untraced:
        details: list[str] = []
        if stale:
            details.append(
                "the following changed sources have an unmodified or metadata/history-only "
                "sidecar body: " + _joined_sample(stale)
            )
        if untraced:
            details.append(
                "the following changed sources have a sidecar body update without a new "
                "Update History entry: " + _joined_sample(untraced)
            )
        raise RuntimeError(
            "external-memory closeout requires updated onboarding content for changed source "
            "files, not only refreshed verification metadata; "
            + "; ".join(details)
            + ". Update each sidecar body through the c-05-create-or-update-onboarding-files "
            "skill and record the change in its Update History, or publish an exact "
            "candidate-bound no-content-impact judgment through curator_coherence. Advancing "
            "lastVerifiedCommitHash on stale content is a prohibited metadata-only refresh."
        )
    return classification["attested_no_impact"]


def validate_onboarding_refresh_plan_for_context(
    context,
    changed_paths: list[str],
    *,
    working_paths: list[str] | None = None,
    body_gate: OnboardingBodyGateEvidence | None = None,
) -> OnboardingRefreshPlan:
    evidence = body_gate or OnboardingBodyGateEvidence()
    plan = onboarding_refresh_plan_for_context(context, changed_paths, working_paths=working_paths)
    missing = plan["missing"]
    unsupported = plan["unsupported"]
    if missing or unsupported:
        details: list[str] = []
        if missing:
            details.append(f"missing sidecar onboarding for: {', '.join(missing)}")
        if unsupported:
            details.append(f"unsupported onboarding storage for: {', '.join(unsupported)}")
        raise RuntimeError(
            "external-memory closeout requires current onboarding for changed source files before memory commit; "
            + "; ".join(details)
            + ". Run the c-05-create-or-update-onboarding-files skill, then rerun closeout."
        )
    require_updated_sidecar_content(
        context,
        plan,
        memory_tree=evidence.memory_tree,
        memory_verified_commit=evidence.memory_verified_commit,
        accepted_no_impact=evidence.accepted_no_impact,
    )
    for item in plan["required"]:
        onboarding_path = Path(item["onboarding_file"])
        text = filesystem.read_text(onboarding_path, encoding="utf-8")
        if "lastVerifiedCommitHash" not in text or "lastVerifiedCommitDate" not in text:
            raise RuntimeError(
                "external-memory closeout requires onboarding verification metadata before memory commit; "
                f"{onboarding_path.as_posix()} is missing lastVerifiedCommitHash or lastVerifiedCommitDate. "
                "Run the c-05-create-or-update-onboarding-files skill, then rerun closeout."
            )
    return plan


def validate_onboarding_refresh_plan(
    contract: WorktreeContract,
    changed_paths: list[str],
    *,
    working_paths: list[str] | None = None,
    accepted_no_impact: frozenset[str] = frozenset(),
) -> OnboardingRefreshPlan:
    return validate_onboarding_refresh_plan_for_context(
        contract_context(contract),
        changed_paths,
        working_paths=working_paths,
        body_gate=OnboardingBodyGateEvidence(
            memory_tree=contract.memory_worktree,
            memory_verified_commit=contract_memory_verified_commit(contract),
            accepted_no_impact=accepted_no_impact,
        ),
    )


def refresh_onboarding_metadata_for_context(
    context,
    change: VerifiedChange,
    *,
    memory_tree: Path | None = None,
    memory_verified_commit: str = "",
    accepted_no_impact: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    verified_commit = change.commit
    verified_date = change.commit_date
    plan = validate_onboarding_refresh_plan_for_context(
        context,
        change.changed_paths,
        working_paths=change.working_paths,
        body_gate=OnboardingBodyGateEvidence(
            memory_tree=memory_tree,
            memory_verified_commit=memory_verified_commit,
            accepted_no_impact=accepted_no_impact,
        ),
    )
    refreshed: list[dict[str, str]] = []
    for item in plan["required"]:
        onboarding_path = Path(item["onboarding_file"])
        text = filesystem.read_text(onboarding_path, encoding="utf-8")
        text, hash_found = onboarding_metadata_row(
            text, "lastVerifiedCommitHash", verified_commit, code=True
        )
        text, date_found = onboarding_metadata_row(text, "lastVerifiedCommitDate", verified_date)
        if not hash_found or not date_found:
            raise RuntimeError(
                "external-memory closeout requires onboarding verification metadata before memory commit; "
                f"{onboarding_path.as_posix()} is missing lastVerifiedCommitHash or lastVerifiedCommitDate. "
                "Run the c-05-create-or-update-onboarding-files skill, then rerun closeout."
            )
        filesystem.write_text(onboarding_path, text, encoding="utf-8")
        refreshed.append(item)
    refreshed.extend(
        _refresh_regenerated_documents(
            context,
            memory_tree=memory_tree,
            verified_commit=verified_commit,
            verified_date=verified_date,
            already={item["onboarding_file"] for item in refreshed},
        )
    )
    return refreshed


def _refresh_regenerated_documents(
    context,
    *,
    memory_tree: Path | None,
    verified_commit: str,
    verified_date: str,
    already: set[str],
) -> list[dict[str, str]]:
    """Stamp the task's regenerated citation documents to the new commit as well.

    The citation gate clears a changed claim by making its citation CURRENT (the fixer
    regenerates the range against the working tree), so a document can be fully reviewed and
    repaired without its own source file changing. Those documents are outside the
    changed-source plan; stamping only the plan would leave their fresh ranges pinned to a
    commit whose constructs no longer exist. Any onboarding document the task touched (memory
    worktree diff) that carries verification metadata advances here, except route overviews and
    entity catalogs, which have their own refresh passes.
    """
    if memory_tree is None:
        return []
    onboarding_root = context.onboarding_root
    refreshed: list[dict[str, str]] = []
    status = run_git(memory_tree, ["status", "--porcelain"])
    if status.returncode != 0:
        return []
    # Porcelain is "XY<space>path" and the leading column is significant: read raw stdout
    # (a stripped runner would eat the first entry's leading space and drop that file).
    for line in status.stdout.splitlines():
        relative = line[3:]
        if not relative.startswith("onboarding/"):
            continue
        relative = relative[len("onboarding/") :]
        if (
            not relative.endswith(".md")
            or relative.endswith(("overview.md", "entities.md", "memory.md"))
            or not (onboarding_root / relative).exists()
        ):
            continue
        document = onboarding_root / relative
        if document.as_posix() in already:
            continue
        text = filesystem.read_text(document, encoding="utf-8")
        if "lastVerifiedCommitHash" not in text:
            continue
        text, hash_found = onboarding_metadata_row(
            text, "lastVerifiedCommitHash", verified_commit, code=True
        )
        text, date_found = onboarding_metadata_row(text, "lastVerifiedCommitDate", verified_date)
        if not hash_found or not date_found:
            continue
        filesystem.write_text(document, text, encoding="utf-8")
        refreshed.append(
            {
                "source_path": "",
                "onboarding_file": document.as_posix(),
            }
        )
    return refreshed


def refresh_onboarding_metadata(
    contract: WorktreeContract,
    change: VerifiedChange,
    *,
    accepted_no_impact: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    return refresh_onboarding_metadata_for_context(
        contract_context(contract),
        change,
        memory_tree=contract.memory_worktree,
        memory_verified_commit=contract_memory_verified_commit(contract),
        accepted_no_impact=accepted_no_impact,
    )
