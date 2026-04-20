# Normalized Candidate Template

Use this template when rewriting raw requirement intake into the `Normalized Candidates` section of `requirement_change_candidates.md`.

```markdown
### REQ-<n>

- Requirement type: <change|preservation|constraint|success-check|non-goal|assumption>
- Scope group or functional slice: <scope grouping>
- Normalized requirement: <stable normalized requirement record>
- Source basis: <developer statement|ticket|onboarding|code|canonical doc>
- Affected surfaces if known: <paths, areas, or none-yet>
- Confidence: <high|medium|low>
- Verification intent: <how later Research or Design should confirm this candidate>
- Status: <normalized|questioned|evidence-pending|approved|rejected>
- Promotion link: <requirements.md anchor or pending>
```

## Evidence-Pending Template

Use this when an item cannot be normalized safely yet:

```markdown
### EP-<n>

- Source basis: <developer statement|ticket|onboarding|code|canonical doc>
- Candidate summary: <requirement candidate waiting for later research>
- Blocked by: <missing evidence or ambiguity>
- Next trigger: <what later Research re-entry should resolve>
- Status: evidence-pending
```

## Raw Intake Reminder

Raw entries remain in the `Raw Intake` section with status `raw` until this skill normalizes them or explicitly moves them to `Evidence Pending`.

## Usage Rules

1. Suggested record fields from the canon are: requirement ID, scope group or functional slice, requirement type, source basis, affected surfaces if known, confidence, verification intent, status, and promotion link.
2. Normalized candidates are not approved requirements yet.
3. Approved items are promoted into `requirements.md` later and removed from `requirement_change_candidates.md`.
4. Rejected items are removed after their disposition is recorded in the owning clarification artifact.
5. Evidence-pending items stay staged until a later Research re-entry resolves them.
6. Open questions are not a candidate type.