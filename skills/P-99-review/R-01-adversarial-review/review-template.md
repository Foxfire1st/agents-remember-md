# CP<N> <Phase> Review

| Field | Value |
| --- | --- |
| task | `<task-folder-name>` |
| checkpoint | `cp<n>-<phase>` |
| verdict | `pass` / `pass-with-concerns` / `fail` |
| status | `review-complete` |
| lastUpdated | `<YYYY-MM-DDThh:mm>` |

## Verdict Summary

1 to 3 sentences covering overall quality, whether the checkpoint is safe to pass, and the main reason for the verdict.

## Review Scope

- List the artifacts reviewed for this checkpoint.
- Call out any expected artifact that was missing.

## Continuity Check

| Upstream artifact | Carried forward? | Notes |
| --- | --- | --- |
| `task.md` | `yes` / `partial` / `no` | |
| `<other artifact>` | `yes` / `partial` / `no` | |

## Completeness Assessment

| Expected deliverable | Present? | Substantive? | Notes |
| --- | --- | --- | --- |
| `<artifact>` | `yes` / `no` | `yes` / `thin` / `placeholder` | |

## Findings

### Blockers

1. **<finding-title>**
     - **Evidence:** <quote, path, or concrete observation>
     - **Impact:** <why forward movement should stop>

### Concerns

1. **<finding-title>**
     - **Evidence:** <quote, path, or concrete observation>
     - **Risk:** <what could go wrong>

### Notes

1. **<finding-title>**
     - **Suggestion:** <clarity or quality improvement>

## Adversarial Checks Performed

| Check | Applied? | Finding |
| --- | --- | --- |
| Cross-reference tracing | `yes` / `no` | |
| Assumption surfacing | `yes` / `no` | |
| Counter-scenario generation | `yes` / `no` | |
| Scope boundary probing | `yes` / `no` | |
| Known-unknowns accounting | `yes` / `no` | |

## Human Review Outcome

- Record whether the developer accepted the checkpoint, requested changes, overrode a failure, or paused for discussion.

## Checkpoint Disposition

- State the next workflow action after this review.

## Developer Questions

1. <question for the developer, if any>
