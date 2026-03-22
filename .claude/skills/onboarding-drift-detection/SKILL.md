---
name: onboarding-drift-detection
description: "Detect when code files have changed since their onboarding documentation was last verified. Uses git diff against lastVerifiedCommitHash to find stale onboarding, then guides updates. Usable standalone or invoked by the task-workflow skill at task start and end."
---

# Onboarding Drift Detection

Detect when code files have changed since their onboarding documentation was last verified, and guide updates to bring onboarding back in sync. This prevents the agent from planning or implementing against stale documentation.

---

## Prerequisites

- Git must be available in the workspace.
- Onboarding files must follow the template from `create_or_update-onboarding-file` skill — specifically, they must have `lastVerifiedCommitHash` and `lastVerifiedCommitDate` in their header block.
- The available time tool must be available for timestamps.

---

## Inputs

The skill accepts a **scope parameter** — one of:

1. **Specific files** — a list of source file paths. The skill finds their corresponding onboarding MDs and checks for drift.
2. **A component** — e.g., "dashboard". The skill checks all onboarding files under that component's directory.
3. **A repo** — e.g., "api-service". The skill checks all onboarding files for that repo.
4. **"all"** — checks every onboarding file in the workspace. Use for periodic maintenance.

When invoked from the task-workflow skill, the scope is typically the set of files in scope for the task.

---

## Workflow

### Step 1: Identify onboarding files in scope

Based on the scope parameter, locate the relevant onboarding MDs:

```bash
# For specific files — find the corresponding onboarding MD
# Source: api-service/src/controllers/SessionController.ts
# Onboarding: onboarding/api-service/core/src/controllers/SessionController.md

# For a component — list all MDs under the component
find onboarding/<repo>/<component>/ -name "*.md" -not -name "overview.md"

# For a repo — list all file-level MDs
find onboarding/<repo>/ -name "*.md" -not -name "overview.md" -not -name "index.md"

# For "all" — list everything
find onboarding/ -name "*.md" -not -name "overview.md" -not -name "index.md"
```

### Step 2: Extract metadata from each onboarding file

For each onboarding MD, read the header and extract:
- `path:` — the source file path (repo-relative).
- `repository:` — which repo the source file belongs to.
- `lastVerifiedCommitHash:` — the commit hash when onboarding was last verified.
- `lastVerifiedCommitDate:` — the date of that commit.

**If `lastVerifiedCommitHash` is empty:** This is an onboarding file for planned code that may not exist yet. Check if the source file now exists:
```bash
test -f <repo-root>/<path> && echo "exists" || echo "not found"
```
If it exists, it was created but the onboarding was never updated with a commit hash — flag it as needing a full update.

### Step 3: Check for drift

For each file with a non-empty `lastVerifiedCommitHash`:

```bash
cd <repo-root>
git diff <lastVerifiedCommitHash> HEAD -- <source-file-path>
```

**Three outcomes:**

1. **No diff** — the source file hasn't changed since the onboarding was verified. No action needed.
2. **Diff exists** — the source file changed. The onboarding is potentially stale.
3. **Commit not found** — the hash doesn't exist in the current repo history (force push, rebase). Treat as potentially stale.

### Step 4: Assess and report drift

Compile a drift report:

```markdown
## Onboarding Drift Report

### Scope: <what was checked>

**Files checked:** <N>
**Up to date:** <N>
**Drifted:** <N>
**Missing hash (planned files):** <N>

### Drifted files

| Onboarding MD | Source file | Last verified | Lines changed |
|---|---|---|---|
| `onboarding/.../File.md` | `repo/src/.../File.ext` | <date> (<short hash>) | +X / -Y |

### Details per drifted file

#### <Source file 1>
**Last verified:** <date> (<hash>)
**Changes since:**
<summary of what changed — key functions modified, logic changes, new methods, removed code>

**Onboarding sections likely affected:**
- Logic — <yes/no, why>
- Invariants — <yes/no, why>
- Conventions — <yes/no, why>
- Cross-repo References — <yes/no, why>
```

### Step 5: Update drifted onboarding files

For each drifted file:

1. **Read the current source file** — understand what changed.
2. **Read the current onboarding MD** — understand what's documented.
3. **Update the onboarding MD** using the `create_or_update-onboarding-file` skill conventions:
   - Update `### Logic` if the code's behaviour changed.
   - Update `### Invariants` if constraints were added, removed, or modified.
   - Update `### Conventions` if patterns changed.
   - Update `### Cross-repo References` if external interfaces changed.
   - Update `### Todos` if new issues are apparent.
4. **Update metadata:**
   ```bash
   # Get the latest commit for this file
   cd <repo-root>
   git log --oneline -1 --format="%H %ci" -- <source-file-path>
   ```
   - Set `lastVerifiedCommitHash` to the latest commit hash.
   - Set `lastVerifiedCommitDate` to the commit date (YYYY-MM-DD).
   - Set `lastUpdated` to today's date (use the available time tool).

### Step 6: Post-commit hash update (task-end mode)

When invoked at the end of a task (after a commit has been made), the workflow is simpler:

1. Get the new commit hash:
   ```bash
   git rev-parse HEAD
   ```

2. For each onboarding file affected by the task:
   - The Code Commentary should already be up-to-date (maintained throughout the task).
   - Update `lastVerifiedCommitHash` to the new commit hash.
   - Update `lastVerifiedCommitDate` to the commit date.
   - Update `lastUpdated` to today's date.

This is a metadata-only pass — the content was already maintained during implementation.

---

## When invoked from the task-workflow skill

| Point                | Mode              | What happens                                                                                         |
| -------------------- | ----------------- | ---------------------------------------------------------------------------------------------------- |
| **Task start**       | Full drift check  | Steps 1–5: find drifted files in scope, review diffs, update onboarding content and metadata.        |
| **Task end**         | Hash stamp        | Step 6: after the commit, update `lastVerifiedCommitHash` on all affected onboarding files.          |

---

## Standalone usage

Can be invoked independently for:
- **Periodic maintenance** — "Check all onboarding for drift" with scope "all".
- **Post-merge cleanup** — after merging a branch, check the affected component's onboarding.
- **Bulk update** — when the developer knows multiple files changed outside the task workflow.
- **Audit** — verify onboarding accuracy before starting a major project.

---

## Error Handling

- **Git not available:** Report error, cannot proceed without git.
- **Source file deleted:** The code file no longer exists but the onboarding MD does. Flag for cleanup: "Source file deleted — onboarding MD should be removed."
- **Source file moved:** Git may detect a rename. If the onboarding path doesn't match the new source path, flag for rename: "Source file moved — onboarding MD path needs updating."
- **Very large diffs:** For files with extensive changes (>100 lines changed), present a summary rather than the full diff. Focus on structural changes (new/removed functions, changed signatures).
- **Commit hash not in history:** Treat as fully drifted — re-read the entire source file and verify the full onboarding content.
