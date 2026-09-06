"""Validate explicit source applicability and conditional execution dependencies."""

from __future__ import annotations

import re

from agents_remember.certification.repository_profiles.validation_primitives import (
    _duplicates,
    _finding,
)


def source_placeholders(rail, rails):
    declarations = [rail.sourceApplicability] if rail.sourceApplicability is not None else []
    declarations.extend(
        predecessor.sourceApplicability
        for identity in rail.conditionalPrerequisites
        if (predecessor := rails.get(identity.key)) is not None
        and predecessor.sourceApplicability is not None
    )
    return {"source-selection:" + item.selectorId for item in declarations}


def validate_source_applicability(profile, rails, findings):
    declarations = [
        rail.sourceApplicability for rail in profile.rails if rail.sourceApplicability is not None
    ]
    _duplicates(
        [item.selectorId for item in declarations], "source-selector-duplicate", "rails", findings
    )
    _duplicates(
        [item.evidencePath for item in declarations],
        "source-selector-evidence-duplicate",
        "rails",
        findings,
    )
    publications = {item.path: item for item in profile.publishedArtifacts}
    for rail in profile.rails:
        path = f"rails.{rail.identity.key}"
        _validate_conditional(rail, rails, path, findings)
        declaration = rail.sourceApplicability
        if declaration is not None:
            published = publications.get(declaration.evidencePath)
            if published is None or rail.gate not in published.publisherGates:
                findings.append(
                    _finding(
                        "source-selection-publication-missing",
                        path,
                        "source applicability requires a declared same-gate evidence publication",
                    )
                )
            if rail.execution.skippedExitCodes:
                findings.append(
                    _finding(
                        "source-selection-posthoc-skip",
                        path,
                        "source applicability is admitted before execution and cannot be reclassified by a skipped exit",
                    )
                )
        used = {
            value
            for token in rail.execution.command
            for value in re.findall(r"\{([^{}]+)\}", token)
            if value.startswith("source-selection:")
        }
        if used != source_placeholders(rail, rails):
            findings.append(
                _finding(
                    "source-selection-command-mismatch",
                    path,
                    "the command must consume exactly its own and declared conditional source-selection inputs",
                )
            )


def _validate_conditional(rail, rails, path, findings):
    _duplicates(
        [item.key for item in rail.conditionalPrerequisites],
        "conditional-prerequisite-duplicate",
        path,
        findings,
    )
    for identity in rail.conditionalPrerequisites:
        predecessor = rails.get(identity.key)
        if (
            identity not in rail.prerequisites
            or predecessor is None
            or predecessor.gate != rail.gate
            or predecessor.sourceApplicability is None
        ):
            findings.append(
                _finding(
                    "conditional-prerequisite-invalid",
                    path,
                    "conditional execution dependency must name a declared same-gate source applicability owner",
                )
            )
