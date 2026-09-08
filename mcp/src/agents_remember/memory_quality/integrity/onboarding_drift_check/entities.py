"""Repo entity-catalog fingerprint classification for drift detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agents_remember.kernel.coordination_context_resolver import (
    StorageSettings,
    clean_scalar,
    normalize_rel_path,
)
from agents_remember.memory_quality.integrity.onboarding_drift_check.discovery import (
    parse_table_metadata,
    rel,
)
from agents_remember.memory_quality.integrity.onboarding_drift_check.git_ops import (
    compute_git_blob_set_fingerprint,
    entity_local_change_notes,
)
from agents_remember.memory_quality.integrity.onboarding_drift_check.models import (
    GIT_BLOB_SET_ALGORITHM,
    DriftRow,
    EntityFingerprint,
)


@dataclass(frozen=True)
class EntityCatalog:
    """The repo entity catalog being classified.

    Its onboarding file, the onboarding root every emitted row is relative to, the repository
    it documents, the storage settings that stamp each row, and the ``lastUpdated`` date the
    rows inherit. All five are read out of one document before any row is emitted, and every
    row builder needs all five -- so the catalog travels as the document it is.
    """

    onboarding_file: Path
    onboarding_root: Path
    repository: str
    settings: StorageSettings
    last_updated: str


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def split_evidence_paths(value: str) -> list[str]:
    normalized = re.sub(r"<br\s*/?>", ";", value, flags=re.IGNORECASE)
    paths: list[str] = []
    for raw_path in normalized.split(";"):
        source_path = clean_scalar(raw_path).strip().strip("`")
        if not source_path or source_path.lower() in {"n/a", "none"}:
            continue
        paths.append(normalize_rel_path(source_path))
    return paths


def _is_table_separator_row(cells: list[str]) -> bool:
    return all(set(cell) <= {"-", ":"} for cell in cells)


def _normalized_header_cells(cells: list[str]) -> list[str]:
    return [re.sub(r"\s+", "", cell).lower() for cell in cells]


def _entity_fingerprint_from_row(headers: list[str], cells: list[str]) -> EntityFingerprint | None:
    row = {
        header: cells[index] if index < len(cells) else "" for index, header in enumerate(headers)
    }
    entity = clean_scalar(row.get("entity", "")).strip()
    if not entity:
        return None
    return EntityFingerprint(
        entity=entity,
        algorithm=clean_scalar(row.get("algorithm", "")).strip("`"),
        fingerprint=clean_scalar(row.get("fingerprint", "")).strip("`"),
        evidence_paths=split_evidence_paths(row.get("evidencepaths", "")),
    )


def parse_entity_fingerprint_rows(path: Path) -> list[EntityFingerprint]:
    lines = path.read_text(encoding="utf-8").splitlines()
    in_section = False
    headers: list[str] = []
    rows: list[EntityFingerprint] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            in_section = stripped == "## Entity Fingerprints"
            headers = []
            continue
        if not in_section:
            continue
        if not stripped.startswith("|"):
            if headers:
                break
            continue
        cells = split_table_row(stripped)
        if _is_table_separator_row(cells):
            continue
        normalized_cells = _normalized_header_cells(cells)
        if {"entity", "algorithm", "fingerprint", "evidencepaths"}.issubset(set(normalized_cells)):
            headers = normalized_cells
            continue
        if not headers:
            continue
        fingerprint = _entity_fingerprint_from_row(headers, cells)
        if fingerprint is not None:
            rows.append(fingerprint)
    return rows


def parse_entity_inventory_names(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    in_section = False
    names: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            in_section = stripped == "## Entity Inventory"
            continue
        if not in_section or not stripped.startswith("### "):
            continue
        name = clean_scalar(stripped.removeprefix("###").strip()).strip("`")
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


@dataclass(frozen=True)
class CensusEntityRow:
    """Exact inventory key, curated evidence, and row bytes for structural comparison."""

    key: str
    evidence_paths: tuple[str, ...]
    inventory_text: str
    fingerprint_text: str


def _census_entity_key(cell: str) -> str:
    key = cell.strip()
    if len(key) >= 2 and key.startswith("`") and key.endswith("`"):
        key = key[1:-1]
    if not key or "\x00" in key:
        raise ValueError("entity census requires a nonempty decoded Entity key")
    key.encode("utf-8", errors="strict")
    return key


def _census_evidence_paths(fields: dict[str, str]) -> tuple[str, ...]:
    if "evidencepaths" not in fields:
        raise ValueError("entity fingerprint table lacks evidencePaths")
    cells = re.sub(r"<br\s*/?>", ";", fields["evidencepaths"], flags=re.IGNORECASE).split(";")
    evidence = []
    for cell in cells:
        value = cell.strip()
        if not value or value.lower().strip("`") in {"n/a", "none"}:
            continue
        evidence.append(_census_entity_key(value))
    return tuple(evidence)


@dataclass
class _EntityCensusParser:
    inventory: dict[str, list[str]]
    fingerprints: dict[str, tuple[str, tuple[str, ...]]]
    section: str = ""
    headers: tuple[str, ...] = ()
    current: str | None = None

    def inventory_entry(self, key: str, raw: str) -> None:
        if key in self.inventory:
            raise ValueError(f"duplicate entity inventory key: {key}")
        self.inventory[key] = [raw]

    def consume(self, raw: str) -> None:
        line = raw.strip()
        if line.startswith("## "):
            self.section = line
            self.headers = ()
            self.current = None
            return
        if self.section == "## Entity Inventory":
            if line.startswith("### "):
                self.current = _census_entity_key(line.removeprefix("### "))
                self.inventory_entry(self.current, raw)
                return
            if self.current is not None:
                self.inventory[self.current].append(raw)
                return
        if self.section in {"## Entity Inventory", "## Entity Fingerprints"} and line.startswith(
            "|"
        ):
            self.table_row(line, raw)

    def table_row(self, line: str, raw: str) -> None:
        cells = split_table_row(line)
        if _is_table_separator_row(cells):
            return
        normalized = _normalized_header_cells(cells)
        if "entity" in normalized:
            if len(normalized) != len(set(normalized)):
                raise ValueError("duplicate entity catalog table header")
            self.headers = tuple(normalized)
            return
        if not self.headers:
            raise ValueError("entity catalog row precedes its Entity header")
        if len(cells) != len(self.headers):
            raise ValueError("entity catalog row does not match its header width")
        fields = dict(zip(self.headers, cells, strict=True))
        key = _census_entity_key(fields["entity"])
        if self.section == "## Entity Inventory":
            self.inventory_entry(key, raw)
            return
        if key in self.fingerprints:
            raise ValueError(f"duplicate entity fingerprint key: {key}")
        self.fingerprints[key] = (raw, _census_evidence_paths(fields))

    def rows(self) -> tuple[CensusEntityRow, ...]:
        if set(self.inventory) != set(self.fingerprints):
            raise ValueError("entity inventory and fingerprint keys must match exactly once")
        return tuple(
            CensusEntityRow(
                key,
                self.fingerprints[key][1],
                "".join(self.inventory[key]),
                self.fingerprints[key][0],
            )
            for key in sorted(self.inventory, key=lambda item: item.encode("utf-8"))
        )


def parse_entity_census_rows(text: str) -> tuple[CensusEntityRow, ...]:
    """Parse exact keys without the drift reader's historical deduplication.

    Both canonical inventory forms (named subsections and Entity tables) retain
    duplicate keys so ambiguity refuses instead of silently selecting a row.
    """
    parser = _EntityCensusParser({}, {})
    for raw in text.splitlines(keepends=True):
        parser.consume(raw)
    return parser.rows()


@dataclass(frozen=True)
class _EntityVerdict:
    """The four fields that differ between one entity classification and the next.

    Every ``DriftRow`` this module builds carries the same six identifying fields, derived
    from the catalog and the row; only these four say what was found. Naming them is what
    turned six twelve-line constructions into six statements of the finding.
    """

    classification: str
    trust: str
    affected_sections: str
    note: str


def _entity_drift_row(
    catalog: EntityCatalog, row: EntityFingerprint, verdict: _EntityVerdict
) -> DriftRow:
    """One classified entity row, with the identifying fields filled in once."""
    return DriftRow(
        onboarding_file=rel(catalog.onboarding_file, catalog.onboarding_root),
        source_file=f"entity:{row.entity}",
        repository=catalog.repository,
        storage_mode=catalog.settings.mode,
        last_verified_hash=row.fingerprint,
        last_verified_date=catalog.last_updated,
        classification=verdict.classification,
        trust=verdict.trust,
        affected_sections=verdict.affected_sections,
        note=verdict.note,
    )


def _structurally_invalid_entity(
    catalog: EntityCatalog,
    repo_root: Path,
    row: EntityFingerprint,
) -> DriftRow | None:
    """Classify rows the HEAD comparison cannot be run against, or ``None`` if it can.

    Three shapes: an algorithm this checker does not implement, a row with nothing to
    compare, and evidence that has since been deleted or moved. None of them is a
    fingerprint mismatch, and calling them one would send the reader looking for a code
    change that never happened.
    """
    if row.algorithm != GIT_BLOB_SET_ALGORITHM:
        return _entity_drift_row(
            catalog,
            row,
            _EntityVerdict(
                classification="unsupported",
                trust="low",
                affected_sections=f"entity catalog; {row.entity}",
                note=f"Unsupported entity fingerprint algorithm '{row.algorithm}'.",
            ),
        )
    if not row.fingerprint or not row.evidence_paths:
        return _entity_drift_row(
            catalog,
            row,
            _EntityVerdict(
                classification="missing verification",
                trust="medium",
                affected_sections=f"entity catalog; {row.entity}",
                note="Entity fingerprint row is missing a fingerprint value or evidence paths.",
            ),
        )
    missing_paths = [
        source_path for source_path in row.evidence_paths if not (repo_root / source_path).exists()
    ]
    if not missing_paths:
        return None
    return _entity_drift_row(
        catalog,
        row,
        _EntityVerdict(
            classification="drifted",
            trust="low",
            affected_sections=f"entity catalog; {row.entity}; source evidence",
            note=(
                f"Entity evidence path missing: {', '.join(missing_paths)}. "
                "Check whether the entity was removed, renamed, or moved before deleting or replacing the fingerprint evidence."
            ),
        ),
    )


def classify_entity_fingerprint(
    catalog: EntityCatalog,
    repo_root: Path,
    row: EntityFingerprint,
) -> DriftRow:
    """Classify one entity fingerprint against the evidence it was computed from."""
    early = _structurally_invalid_entity(catalog, repo_root, row)
    if early is not None:
        return early
    try:
        current = compute_git_blob_set_fingerprint(repo_root, row.evidence_paths)
    except RuntimeError as error:
        return _entity_drift_row(
            catalog,
            row,
            _EntityVerdict(
                classification="drifted",
                trust="low",
                affected_sections=f"entity catalog; {row.entity}; source evidence",
                note=f"Unable to compute entity fingerprint: {error}",
            ),
        )

    local_notes = entity_local_change_notes(repo_root, row.evidence_paths)
    if current == row.fingerprint and not local_notes:
        return _entity_drift_row(
            catalog,
            row,
            _EntityVerdict(
                classification="up to date",
                trust="high",
                affected_sections="none",
                note="Entity evidence fingerprint matches current HEAD.",
            ),
        )
    if current == row.fingerprint:
        return _entity_drift_row(
            catalog,
            row,
            _EntityVerdict(
                classification="drifted",
                trust="medium",
                affected_sections=f"entity catalog; {row.entity}; source evidence",
                note="; ".join(local_notes),
            ),
        )
    note = "Entity evidence fingerprint changed since the catalog was refreshed."
    if local_notes:
        note = f"{note} Local changes also exist: {'; '.join(local_notes)}"
    return _entity_drift_row(
        catalog,
        row,
        _EntityVerdict(
            classification="drifted",
            trust="medium",
            affected_sections=f"entity catalog; {row.entity}; source evidence",
            note=note,
        ),
    )


def missing_entity_fingerprint_row(catalog: EntityCatalog, entity: str, note: str) -> DriftRow:
    return DriftRow(
        onboarding_file=rel(catalog.onboarding_file, catalog.onboarding_root),
        source_file=f"entity:{entity}",
        repository=catalog.repository,
        storage_mode=catalog.settings.mode,
        last_verified_hash="",
        last_verified_date=catalog.last_updated,
        classification="missing verification",
        trust="medium",
        affected_sections=f"entity catalog; {entity}; verification",
        note=note,
    )


def orphaned_entity_fingerprint_row(catalog: EntityCatalog, row: EntityFingerprint) -> DriftRow:
    return DriftRow(
        onboarding_file=rel(catalog.onboarding_file, catalog.onboarding_root),
        source_file=f"entity:{row.entity}",
        repository=catalog.repository,
        storage_mode=catalog.settings.mode,
        last_verified_hash=row.fingerprint,
        last_verified_date=catalog.last_updated,
        classification="orphaned",
        trust="low",
        affected_sections=f"entity catalog; {row.entity}; verification",
        note="Entity fingerprint row has no matching inventory entry. Check whether the entity was removed, renamed, or moved before deleting the row.",
    )


def classify_entity_catalog(
    onboarding_file: Path, repo_root: Path, onboarding_root: Path, settings: StorageSettings
) -> list[DriftRow]:
    metadata = parse_table_metadata(onboarding_file)
    repository = metadata.get("repository", repo_root.name)
    last_updated = metadata.get("lastUpdated", "")
    catalog = EntityCatalog(
        onboarding_file=onboarding_file,
        onboarding_root=onboarding_root,
        repository=repository,
        settings=settings,
        last_updated=last_updated,
    )
    inventory_entities = parse_entity_inventory_names(onboarding_file)
    rows = parse_entity_fingerprint_rows(onboarding_file)
    if not rows:
        if inventory_entities:
            return [
                missing_entity_fingerprint_row(
                    catalog,
                    entity,
                    "Repo entity catalog has no parseable Entity Fingerprints table for this inventory entry.",
                )
                for entity in inventory_entities
            ]
        return [
            DriftRow(
                onboarding_file=rel(onboarding_file, onboarding_root),
                source_file="entity-catalog",
                repository=repository,
                storage_mode=settings.mode,
                last_verified_hash="",
                last_verified_date=last_updated,
                classification="missing verification",
                trust="medium",
                affected_sections="entity catalog; verification",
                note="Repo entity catalog has no parseable Entity Fingerprints table.",
            )
        ]
    if not inventory_entities:
        return [
            DriftRow(
                onboarding_file=rel(onboarding_file, onboarding_root),
                source_file="entity-catalog",
                repository=repository,
                storage_mode=settings.mode,
                last_verified_hash="",
                last_verified_date=last_updated,
                classification="missing verification",
                trust="medium",
                affected_sections="entity catalog; inventory; verification",
                note="Repo entity catalog has fingerprint rows but no parseable Entity Inventory section.",
            )
        ]
    fingerprint_entities = {row.entity for row in rows}
    rows_by_inventory = [
        classify_entity_fingerprint(catalog, repo_root, row)
        if row.entity in inventory_entities
        else orphaned_entity_fingerprint_row(catalog, row)
        for row in rows
    ]
    missing_inventory_rows = [
        missing_entity_fingerprint_row(
            catalog,
            entity,
            "Entity inventory entry has no matching fingerprint row. Add a git-blob-set-v1 row with curated evidence paths before treating it as verified.",
        )
        for entity in inventory_entities
        if entity not in fingerprint_entities
    ]
    return [
        *rows_by_inventory,
        *missing_inventory_rows,
    ]
