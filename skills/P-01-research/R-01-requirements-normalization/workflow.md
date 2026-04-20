# Requirements Normalization Workflow

## Goal

Translate raw intake into stable normalized requirement records that `R-02-input-documentation` can test against the real current system.

## Workflow

1. read the raw-intake entries in `requirement_change_candidates.md`
2. read onboarding, relevant code, and canonical docs, tickets, or prior developer discussion only as needed to stabilize the record
3. read approved requirements when overlap must be checked
4. separate merged, overlapping, or ambiguous asks into distinct candidates
5. rewrite each surviving candidate into clearer requirement language without changing intent casually
6. add the normalized records to `Normalized Candidates`
7. remove those items from `Raw Intake` once they have been normalized

## Normalization Rules

1. Normalization is contract-shaping, not deep area research.
2. Keep the normalized wording implementation-neutral.
3. Preserve requirement-side meaning; do not smuggle architecture direction into the candidate.
4. If one raw item contains multiple independent asks, split it into multiple normalized candidates.
5. If multiple raw items state the same requirement differently, merge them into one normalized candidate and preserve the source basis.
6. If a candidate overlaps with already approved requirements, record that overlap explicitly instead of silently overriding the approved contract.
7. If the available evidence is too weak to produce a stable normalized record, move the item to `Evidence Pending` instead of forcing a premature rewrite.
8. Open questions are not a candidate type; requirement-side uncertainty stays attached through status and Research evidence.

## Output Rules

1. Write back into `requirement_change_candidates.md`.
2. Use the normalized-candidate template for every normalized entry.
3. Keep evidence-pending items in their own section for later Research re-entry.
4. Do not write target-state design, implementation steps, or approval decisions into this step.
5. The orchestrator moves items from raw-intake into the normalized list as part of this step.

## Exit Condition

The requirement side of the task is clean enough that `R-02-input-documentation` can investigate the current system against stable normalized candidates instead of raw phrasing noise.