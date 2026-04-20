# Output Documentation Overview Template

Use this template for the one root `P-03-design/D-04-output-documentation/overview.md` artifact.

```md
# Output Documentation Overview

| Field | Value |
| --- | --- |
| task | `<task-folder-name>` |
| doc_type | `output-documentation-overview` |
| lastUpdated | <YYYY-MM-DDThh:mm> |

## Included Files
- <mapped output doc path>

## Scope-Level Target Flow
## Ownership Boundaries
## Cross-File Change Strategy
## Sequencing Constraints

## Feature Requirements
| Requirement ID | Requirement Text | File Links |
| --- | --- | --- |

## Architectural Requirements
| Architecture ID | Architecture Text | File Links |
| --- | --- | --- |

## Representative Example Index
| Example ID | Output Doc | Distinct Change | Feature Requirements | Architectural Requirements |
| --- | --- | --- | --- | --- |

## Change Log
```

## Rules

1. D-04 writes one root overview, not cluster-specific overviews.
2. The overview must aggregate all approved requirement IDs and architecture IDs with short plain-language summaries and one or more file links each.
3. The overview must index the representative examples available for Planning to pull.
4. The overview synthesizes the projected slice and does not replace file-level docs.