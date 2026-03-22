---
name: create_or_update-onboarding-file
description: "Create and maintain file-level onboarding MDs — the code commentary layer. Covers template, folder organization, section conventions, and metadata. Enforces strict 1-to-1 mapping: one source file = one onboarding MD. Use this whenever creating new onboarding files or auditing existing ones for format compliance."
---

# Create / Maintain File-Level Onboarding MDs

File-level onboarding MDs are **code commentary** — they describe what the code does, why, and what conventions it follows. They are not planning documents (that's what task files are for). They live in `onboarding/` and mirror the source tree of the repo they document.

---

## Folder Organization

### Hierarchy

```
onboarding/
  index.md                              ← workspace-level index
  <repo>/
    overview.md                         ← repo-level overview
    <component>/
      overview.md                       ← component-level overview
      src/                              ← mirrors source tree from here
        <path>/<to>/<file>.md           ← file-level MD
```

### Mirroring Rules

- **Strict 1-to-1 mapping:** every source file gets its own onboarding MD. Never group multiple source files into one MD.
- The folder structure under `<component>/src/` mirrors the repo's source tree exactly.
- The onboarding MD sits in the same relative directory as the source file it documents.
- File name = source file name with `.md` extension (e.g., `UserService.ts` → `UserService.md`).

### Examples

| Source file | Onboarding MD |
|---|---|
| `api-service/src/controllers/SessionController.ts` | `onboarding/api-service/core/src/controllers/SessionController.md` |
| `web-client/src/stores/sessionStore.ts` | `onboarding/web-client/dashboard/src/stores/sessionStore.md` |
| `api-service/src/services/EventPublisher.ts` | `onboarding/api-service/core/src/services/EventPublisher.md` |

---

## Template

```markdown
# FileName.ext
repository: <repo-name>
path: <repo-relative path to source file>
lastUpdated: <YYYY-MM-DD>
lastVerifiedCommitHash: <full 40-char SHA>
lastVerifiedCommitDate: <YYYY-MM-DD>

## Code Commentary

### Logic

<What the code does. Key functions/methods, data flow, algorithms, state machines.
Use bold **`functionName()`** for key functions. Use tables for mappings/enums.
Include enough detail that a reader can understand the file without opening it.>

### Invariants

<Rules that must always hold. Sync points, ordering constraints, uniqueness requirements.
Things that would break if violated.>

### Conventions

<Patterns and style choices specific to this file or area.
Only include if there's something noteworthy beyond standard project conventions.>

### Todos

<Known issues, tech debt, planned improvements that aren't tied to a specific task.
These are observations, not planning — task-linked work goes in the Tasks section.>

### Docs References

<Links to reference documentation that explains the domain concepts this code implements.
Format: `docs/<path>.md` — <brief description>>

### Cross-repo References

<Interfaces to/from other repos: API calls, WS events, message queue topics, shared types.
Format: <what> — <which repo> <where>>

## Tasks

<Task>
target: <what part of this file is affected>
taskGoal: "<migration|deprecation|bugfix|feature>"
progressStatus: "<planned|in-progress|readyForReview|needsFixes|completed>"
description: <what will change and why>
progress: <current state, blockers>
taskfile: tasks/<task-file>.md (<task-id>)
</Task>

## Update History
<!-- newest first; entries move here from Tasks when task file is completed or abandoned -->
```

---

## Section Rules

### Required sections

Every file-level MD **must** have:
- Header block (title, repository, path, lastUpdated, lastVerifiedCommitHash, lastVerifiedCommitDate)
- `## Code Commentary` with at least `### Logic`

### Optional sections

Include only when there's meaningful content:
- `### Invariants` — almost always relevant; omit only for trivial DTOs
- `### Conventions` — only if this file does something noteworthy
- `### Todos` — only if there are known issues not tied to a task
- `### Docs References` — when reference docs exist for this domain
- `### Cross-repo References` — when the code communicates with other repos
- `## Tasks` + `## Update History` — only when a task file references this code

### Section ordering

Sections must appear in this order (skip absent ones):
1. `### Logic`
2. `### Invariants`
3. `### Conventions`
4. `### Todos`
5. `### Docs References`
6. `### Cross-repo References`
7. `## Tasks`
8. `## Update History`

---

## Task Block Format

Task blocks link onboarding files to task files. The task file holds the plan; the onboarding file holds a summary pointer.

```
<Task>
target: <what part of this file is affected>
taskGoal: "<migration|deprecation|bugfix|feature>"
progressStatus: "<planned|in-progress|readyForReview|needsFixes|completed>"
description: <1-2 sentence summary of what changes>
progress: <current state and blockers>
taskfile: tasks/<filename>.md (<task-id>)
</Task>
```

**taskGoal values:**

All values use the goal form (e.g., "deprecation" not "deprecated") — this signals intent, not completion. The `progressStatus` tells you whether the goal has been achieved.

| Value | Meaning |
|---|---|
| `migration` | **Migration target.** This file is the destination being moved towards. Marks what is being built or adopted as part of moving away from an old implementation. |
| `deprecation` | **Migration source / removal target.** This file (or part of it) is being marked for removal because consumers are migrating away. Use `"partial deprecation"` if only some uses are being removed while others remain — in that case the Code Commentary must document exactly which parts are deprecated and which are still active. |
| `bugfix` | **Bug fix.** This file is being changed to correct incorrect behavior. |
| `feature` | **New functionality or extension.** Covers both entirely new code and additions to existing code. |

**progressStatus values:**

| Value | Meaning |
|---|---|
| `planned` | Task is defined but implementation has not started |
| `in-progress` | Currently being implemented |
| `readyForReview` | Implementation done, awaiting human review |
| `needsFixes` | Review done, changes requested — back to implementer |
| `completed` | Reviewed and approved |

**Field definitions:**
| Field | Required | Values |
|---|---|---|
| `target` | Yes | Specific function, section, or scope within the file |
| `taskGoal` | Yes | `migration`, `deprecation`, `bugfix`, `feature` |
| `progressStatus` | Yes | `planned`, `in-progress`, `readyForReview`, `needsFixes`, `completed` |
| `description` | Yes | What will change (not why — that's in the task file) |
| `progress` | Yes | Free text: current state, blockers, dependencies |
| `taskfile` | Yes | Relative path with optional task-id in parens |

**Lifecycle:**
- When a task references this file → add `## Tasks` section with `<Task>` block
- When `progressStatus` reaches `completed` → mark task entry as ready to move to `## Update History`
- **Actual move happens only when the parent task file is completed or abandoned** (with existing changes kept) — not when individual entries complete
- Update History entries are newest-first, each recording: what changed, why, and timestamp:
  ```
  - YYYY-MM-DD — <what changed and why> (task: <taskfile>)
  ```

---

## Metadata Rules

### lastUpdated
The date the onboarding MD was last written or meaningfully edited. Use the available time tool — never guess.

### lastVerifiedCommitHash / lastVerifiedCommitDate
The latest commit that touches the specific source file this MD documents.

**How to get it:**
```bash
git log --oneline -1 --format="%H %ci" -- <source-file-path>
```

The date is the commit date, not the author date. Extract `YYYY-MM-DD` from the output.

This creates a direct link: one MD tracks exactly one source file's commit history. The `onboarding-drift-detection` skill uses this to detect when a source file changed but its onboarding MD wasn't updated.

**For planned code files (not yet created):** Leave `lastVerifiedCommitHash` and `lastVerifiedCommitDate` empty. An empty commit hash signals this is an onboarding file for code that doesn't exist yet. When the code file is created and committed, fill in the hash. During task progression, empty-hash MDs can be checked against the repo to see if the code file now exists.

### When to update metadata
- **Creating a new MD:** set all three fields
- **Editing the MD because code changed:** update all three fields
- **Verifying the MD is still accurate (no edits):** update `lastUpdated` and the commit fields
- **Editing only the Tasks section:** update `lastUpdated` only (commit fields track code, not task state)

---

## Procedure: Creating Onboarding for Existing or Planned Code

This procedure covers both undocumented existing code and code files that don't exist yet but are planned as part of a task. The workflow is the same except for step 3.

### 1. Identify scope
- List all source files in the area — each one gets its own MD
- For planned code: list the files that will be created, using the planned file paths
- Check if an `overview.md` exists for the component; create one if not

### 2. Get metadata
```bash
# Get current time for lastUpdated
# Use the available time tool

# For existing code — get latest commit:
cd <repo-root>
git log --oneline -1 --format="%H %ci" -- <source-file-path>

# For planned code — leave commit fields empty (see Metadata Rules)
```

### 3. Gather content

**For existing code:**
- Read each source file thoroughly before writing its MD
- Note: logic, invariants, conventions, cross-repo calls, known issues
- Check `/docs/` for relevant domain documentation

**For planned code (file doesn't exist yet):**
- Write Code Commentary based on the task file's design: intended behavior, planned interfaces, expected invariants
- Use future-oriented language in Logic (e.g., "Will handle…", "Responsible for…") so it reads as intent, not as describing running code
- Include a `## Tasks` block with `taskGoal: "feature"` or `"migration"` linking to the task file that defines this new file
- Once the code is written and committed: rewrite Code Commentary to describe what actually exists, fill in the commit hash/date, and update language from intent to present tense

### 4. Create the MDs
- One MD per source file — no exceptions
- Follow the template strictly
- Use the section ordering rules
- Only include sections with meaningful content
- Small files (DTOs, enums) still get their own MD — keep it brief

### 5. Update the component overview
- Add or update the file index section in the component's `overview.md`
- List all new MDs with brief descriptions

### 6. Cross-reference check
- Verify Docs References point to existing files in `docs/`
- Verify Cross-repo References name the correct repo and code location
- Check if any cross-repo ties should be added to the glossary

---

## Procedure: Maintaining Existing Onboarding Files

### When code changes
1. Check if the change affects documented logic, invariants, or conventions
2. If yes: update the relevant sections in the onboarding MD
3. Update `lastUpdated`, `lastVerifiedCommitHash`, `lastVerifiedCommitDate`

### When a task starts
1. Add `## Tasks` section (if not present) with a `<Task>` block
2. Set `progressStatus: "planned"` or `"in-progress"`

### When a task completes
1. Mark the `<Task>` entry `progressStatus: "completed"`
2. Update `## Code Commentary` sections to reflect the new code state
3. Update metadata
4. The entry stays in `## Tasks` until the **parent task file** is completed or abandoned
5. When the task file closes → move entry to `## Update History`:
   ```
   - YYYY-MM-DD — <what changed and why> (task: <taskfile>)
   ```

### When code is deleted
1. Delete the mirrored onboarding MD
2. Remove from component overview file index
3. Check for orphaned glossary terms

### When code is renamed/moved
1. Rename/move the onboarding MD to match
2. Update the component overview file index
3. Update any cross-references from other MDs
