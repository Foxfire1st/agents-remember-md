---
description: "Close a task: reconcile code, onboarding, task notes. Update documentation."
agent: agent
tools: [vscode, execute, read, agent, edit, search, web, memory, todo]
---

# Close Task

You are wrapping up a task. This prompt is a launcher — the full procedure lives in the **task-workflow** skill.

## Procedure

1. **Read the task-workflow skill:** Open and read `.github/skills/task-workflow/SKILL.md`.
2. **Follow Phase 4** (Closing a Task):
   - **4.1 — Summary and confirmation:** Present completion summary to the developer.
   - **4.2 — Final onboarding sync:** Verify all Code Commentary reflects the final code state. Confirm all `<Task>` blocks are at `progressStatus: "completed"`. Update `lastUpdated`.
   - **4.3 — Move completed tasks to Update History:** Remove `<Task>` blocks, add entries to `## Update History`.
   - **4.4 — Commit workflow:** Check for pending work → ask the developer → suggest commit message → commit → invoke `onboarding-drift-detection` in hash-stamp mode.
   - **4.5 — Task file finalisation:** Set status to "Completed", add final decision log entry, note follow-up items.
   - **4.6 — Cross-reference check:** Verify component overviews, glossary, instructions files, check for orphaned onboarding.

## Sub-skills to invoke as needed

- `onboarding-drift-detection` — post-commit hash stamping.
- `create_or_update-onboarding-file` — for final onboarding updates and format compliance.
