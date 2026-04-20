---
name: C-02-onboarding-drift-detection
description: "Detect onboarding drift against the canonical onboarding root, classify how trustworthy existing onboarding remains, and hand actionable maintenance work to C-05-create-or-update-onboarding-files."
---

# C-02 Onboarding Drift Detection

Use this skill at task start, before relying on older onboarding for unfamiliar surfaces, and again near closure after approved code changes land.

Its job is to decide whether onboarding is still trustworthy enough to ground the current work and to produce a concrete maintenance worklist when it is not.

## Scope Inputs

This skill can work from one of these scopes:

1. a specific source-file list
2. a component directory
3. a repo
4. all onboarding in the workspace

## Primary Outputs

1. a drift summary or drift report
2. a classification of onboarding as up to date, drifted, missing verification, or orphaned
3. a maintenance worklist for `C-05-create-or-update-onboarding-files`
4. trust guidance for the caller when stale onboarding still contains directional value

## Boundaries

1. This skill detects and classifies drift; it does not rewrite onboarding content itself.
2. It does not replace deep Research.
3. It does not decide requirement or architecture direction.
4. It should qualify stale onboarding rather than silently treating it as trustworthy.

## Procedure

### 1. Identify onboarding files in scope

Locate the relevant onboarding files under `onboarding/<repo>/...`.

Primary drift detection operates on file-level onboarding that mirrors concrete source files and carries verification metadata.

If repo-level entity catalogs or overview files are in scope, treat them as follow-up maintenance surfaces rather than trying to diff them directly against one source file.

### 2. Extract verification metadata

For each file-level onboarding document in scope, read:

1. `repository`
2. `path`
3. `lastVerifiedCommitHash`
4. `lastVerifiedCommitDate`

If the onboarding file is missing the path or repository metadata needed to resolve the source file, classify it as missing verification and flag it for maintenance.

### 3. Compare the source file against the recorded verification point

Use the recorded metadata to classify the current state:

1. If the source file no longer exists, classify the onboarding file as orphaned.
2. If the source file exists but `lastVerifiedCommitHash` is empty, classify it as missing verification.
3. If the hash exists, compare the source file against `HEAD` using git diff.
4. If there is no diff, classify the onboarding file as up to date.
5. If there is a diff, classify it as drifted.
6. If the recorded hash is no longer available in history, treat the onboarding as drifted and require a fuller verification pass.

### 4. Qualify how trustworthy the onboarding still is

For drifted onboarding, record how much directional value remains:

1. high when the drift is small and the old onboarding still explains the surface accurately enough for orientation
2. medium when the onboarding is useful for adjacent context but no longer safe as a direct statement of current behavior
3. low when the source changed so much that the onboarding should not be trusted without refresh

Also note which sections are likely affected:

1. logic
2. invariants and boundaries
3. conventions
4. cross-repo references
5. repo-level entity catalog follow-up when entity shape or naming drift likely changed

### 5. Produce the maintenance artifact

Write a drift report when the scope is large enough that the caller needs a reusable worklist.

Preferred report locations:

1. `onboarding/<repo>/drift-report.md` for repo, component, or file-scoped runs
2. `onboarding/drift-report-all.md` for workspace-wide runs

The report should include:

1. scope checked
2. generated timestamp
3. counts for up to date, drifted, missing verification, and orphaned files
4. an actionable table listing onboarding file, source file, classification, current trust level, and likely affected sections
5. enough summary detail for `C-05-create-or-update-onboarding-files` to refresh the right surfaces without rerunning the scan from scratch

Treat the drift report as a maintenance artifact, not as a long-lived research handoff.

### 6. Hand off to onboarding maintenance

If actionable files exist, hand the worklist to `C-05-create-or-update-onboarding-files`.

The handoff should identify:

1. which onboarding files need refresh
2. which files are orphaned and may need deletion
3. whether related repo-level catalogs or overview files likely need follow-up
4. which stale onboarding can still be used directionally until maintenance finishes

If no actionable files exist, return a clean summary and stop.

## Rules

1. Drift detection remains evidence qualification and maintenance routing, not deep Research.
2. It must use the canonical `onboarding/` root.
3. It hands maintenance work to `C-05-create-or-update-onboarding-files` instead of performing that maintenance itself.
4. Stale onboarding may remain directional evidence until refreshed or disproven, but that trust level must be made explicit.
5. Missing verification metadata is itself actionable drift.
6. Orphaned onboarding should be surfaced clearly rather than left to accumulate silently.
