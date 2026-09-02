"""Deterministic human projection of structured curator-coherence authority."""

from __future__ import annotations

from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceRecord
from agents_remember.models.task_intent import TaskIntentIdentity


def render_curator_coherence(record: CuratorCoherenceRecord) -> str:
    """Render the canonical record without reparsing Markdown into machine truth."""

    lines = [
        f"# {record.leafId} Curator Coherence — {record.deliveryAttempt}",
        "",
        "> Generated projection. The structured authority manifest and record are canonical;",
        "> this Markdown is validated only by its content digest and is never parsed as input.",
        "",
        "## Bound identities",
        "",
        f"- Semantic requirement revision: `{_text(record.semanticRequirementRevision)}`",
        f"- Worker delivery attempt: `{_text(record.deliveryAttempt)}`",
        f"- Pair contract: `{_text(record.pairIdentity.contractPath)}`",
        f"- Pair contract digest: `{record.pairIdentity.contractDigest}`",
        f"- Code root: `{_text(record.pairIdentity.codeRoot)}`",
        f"- Memory root: `{_text(record.pairIdentity.memoryRoot)}`",
        (
            "- Code branches/base: "
            f"`{_text(record.pairIdentity.codeSourceBranch)}` -> "
            f"`{_text(record.pairIdentity.codeWorkBranch)}` @ "
            f"`{record.pairIdentity.codeBaseCommit}`"
        ),
        (
            "- Memory branches/base: "
            f"`{_text(record.pairIdentity.memorySourceBranch)}` -> "
            f"`{_text(record.pairIdentity.memoryWorkBranch)}` @ "
            f"`{record.pairIdentity.memoryBaseCommit}`"
        ),
        f"- Onboarding root: `{_text(record.pairIdentity.onboardingRoot)}`",
        f"- Ledger path: `{_text(record.pairIdentity.ledgerPath)}`",
        f"- Code candidate tree: `{record.codeCandidateTree}`",
        f"- Memory candidate tree: `{record.memoryCandidateTree}`",
        f"- Task topology fingerprint: `{record.taskTopologyFingerprint}`",
        f"- Memory-quality attestation: `{_text(record.attestationPath)}`",
        f"- Attestation digest: `{record.attestationSha256}`",
        f"- Predecessor authority digest: `{record.predecessorAuthorityDigest or 'none'}`",
        f"- Published by: `{_text(record.publishedBy)}`",
        "",
        "## Source-change dispositions",
        "",
        "| Source file | Onboarding file | Classification | Disposition | Rationale | Evidence reference |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if isinstance(record.taskIntent, TaskIntentIdentity):
        lines.insert(
            lines.index(f"- Task topology fingerprint: `{record.taskTopologyFingerprint}`") + 1,
            f"- Task intent: `{record.taskIntent.schema_}` @ `{record.taskIntent.digest}`",
        )
    judgments = {judgment.identity: judgment for judgment in record.judgments}
    for candidate in record.sourceCandidates:
        judgment = judgments[candidate.identity]
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(candidate.sourceFile),
                    _cell(candidate.onboardingFile),
                    _cell(candidate.classification),
                    _cell(judgment.disposition),
                    _cell(judgment.rationale),
                    _cell(f"{judgment.evidenceRef} @ sha256:{judgment.evidenceSha256}"),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Validation contract",
            "",
            "The lifecycle validator requires the exact current code tree, memory tree, task",
            "topology, attestation bytes, candidate identities, record bytes, and projection",
            "digest. Historical generations are audit evidence only; the stable authority",
            "manifest selects the sole live generation.",
            "",
        ]
    )
    return "\n".join(lines)


def _cell(value: str) -> str:
    return _text(value).replace("|", "\\|").replace("\n", "<br>")


def _text(value: str) -> str:
    return value.replace("`", "\\`")
