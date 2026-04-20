# Input Documentation Overview Template

Use this template for one mapped input documentation overview document. Input documentation overview documents are area-level coordination artifacts for the current-state impact map.

Create them in a dedicated overview-generation wave after the relevant file-level input docs and any required reconciliation for the area are already complete.

This overview does not replace file-level input docs. It synthesizes the cross-file truths that those docs reveal.

# <Input Documentation Overview Title>

| Field             | Value                                            |
| ----------------- | ------------------------------------------------ |
| task              | `<task-folder-name>`                             |
| doc_type          | `input-documentation-overview`                   |
| area_name         | `<brief-defined area name or slug>`              |
| repositories      | `<repo-name or comma-separated repo list>`       |
| related_file_docs | `<list or glob of mapped input file docs>`       |
| overview_path     | `input-project-documentation/<area-name>.overview.md` |
| lastUpdated       | <YYYY-MM-DDThh:mm>                               |

## Included Files

- <mapped input file 1>
- <mapped input file 2>
- <mapped input file 3>

## Unchanged But Load-Bearing Files

- <file> — <why it still matters even though no direct code change is expected>

## Current Flow Across The Area

<Describe the current cross-file flow that matters for this area. Focus on the behavior the workflow must understand before trying to design or change anything.>

## Cross-Cutting Invariants

<List the invariants, ordering rules, lifecycle assumptions, authority lines, or coordination rules that span multiple files in this slice.>

## Authority Boundaries

<Explain which file or subsystem currently owns which part of the behavior, data, or coordination within this slice.>

## Requirement Pressure Points

<Describe where the approved requirements press on the slice at a cross-file level.>

## Likely Breakage Seams

<Call out the cross-file seams where naive changes are likely to break existing behavior.>

## Known Unknowns And Scope Edges

<Document unresolved current-state uncertainties, runtime gaps, unclear ownership lines, and boundaries where the mapped area stops.>

## Evidence Basis

<List the file-level input docs, onboarding, code reads, docs, and discussion that support this overview.>

## File Level Requirement Reference List

| Requirement ID | Requirement Description |Scope (none | local | cross-file | task-global)| Mentioned In onboarding(s) |

## Change Log

- <YYYY-MM-DDThh:mm> — <what changed in this overview and why>

## Notes

- This overview belongs at the root of `input-project-documentation/` and should be named `<area-name>.overview.md`.
- Generate it only in the dedicated overview-generation wave, not during first-pass file mapping.
- Start with `Included Files` so coverage is explicit.
- List unchanged-but-load-bearing files before summarizing them.
- Do not duplicate file-level commentary unless the cross-file relationship is the important point.
- The Requirement Reference List has to list all requirements from requirements.md
