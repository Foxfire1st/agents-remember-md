"""Validate the original producer and later consumers of reconstructed environments."""

from __future__ import annotations

from agents_remember.certification.repository_profiles.validation_primitives import (
    _duplicates,
    _finding,
    _validate_gate_set,
)


def _validate_environments(profile, rails, findings) -> None:
    _duplicates(
        [item.environmentId for item in profile.environments],
        "duplicate-environment",
        "environments",
        findings,
    )
    for item in profile.environments:
        _validate_selected_producer(profile, item, findings)
        _validate_environment_artifacts(profile, item, rails, findings)
        if not item.consumingGates or any(gate == 1 for gate in item.consumingGates):
            findings.append(
                _finding(
                    "environment-consumers-invalid",
                    f"environments.{item.environmentId}.consumingGates",
                    "reconstruction consumers must name the later gates that reuse Gate 1",
                )
            )
        _validate_gate_set(
            item.consumingGates, f"environments.{item.environmentId}.consumingGates", findings
        )


def _validate_environment_artifacts(profile, item, rails, findings) -> None:
    """Validate producer census and later reconstruction-proof publications in order."""
    rail = rails.get(item.producerRail.key)
    published = next(
        (entry for entry in profile.publishedArtifacts if entry.path == item.manifestPath), None
    )
    if (
        rail is None
        or rail.gate != 1
        or item.artifactId not in {artifact.artifactId for artifact in rail.outputArtifacts}
        or published is None
        or published.publisherGates != (1,)
        or published.maxBytes != item.maxManifestBytes
    ):
        findings.append(
            _finding(
                "environment-producer-invalid",
                f"environments.{item.environmentId}",
                "environment census requires a declared Gate 1 producer artifact and exact publication bound",
            )
        )
    proof = next(
        (
            entry
            for entry in profile.publishedArtifacts
            if entry.path == item.reconstructionProofPath
        ),
        None,
    )
    if proof is None or proof.publisherGates != item.consumingGates or proof.maxBytes != 4096:
        findings.append(
            _finding(
                "environment-proof-invalid",
                f"environments.{item.environmentId}",
                "reconstruction proof requires its exact later-gate publication and 4096-byte bound",
            )
        )


def _validate_selected_producer(profile, environment, findings):
    for selection in profile.selections:
        producer = selection.gates[0]
        consumes = any(
            gate.gate in environment.consumingGates and gate.status == "applicable"
            for gate in selection.gates
        )
        if consumes and (
            producer.status != "applicable" or environment.producerRail not in producer.railIds
        ):
            findings.append(
                _finding(
                    "environment-producer-not-selected",
                    f"selections.{selection.selectionId}",
                    "every selected environment consumer requires its original Gate 1 producer",
                )
            )
