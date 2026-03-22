---
name: gitlab-ticket-retrieval
description: "EXAMPLE INTEGRATION — Retrieve a GitLab ticket's full context: issue details, comments, linked issues, and knowledge base references. Adapt this skill for your own issue tracker (Jira, Linear, GitHub Issues, etc)."
---

# GitLab Ticket Retrieval (Example Integration)

> **This is an example integration.** It demonstrates how to connect the task workflow to an issue tracker. Adapt the MCP calls and workflow to your own tool (Jira, Linear, GitHub Issues, Azure DevOps, etc).

Retrieve a GitLab issue's full context — title, description, labels, comments, linked issues, and any knowledge base references found within. This skill produces a structured summary suitable for feeding into a task file or for standalone investigation.

---

## Prerequisites

- A GitLab MCP server must be configured and reachable.
- A valid GitLab API token must be configured.

---

## Inputs

The user provides ONE of:
1. **A ticket number** — e.g., `#1588` or just `1588`. The skill uses the default project ID.
2. **A full GitLab URL** — extract the project path and issue IID from the URL.
3. **A project ID + issue number** — when working outside the default project.

**Default project ID:** If the user provides only a ticket number, use the project ID configured in the workspace (check environment variables or ask the user). When in doubt, ask.

---

## Workflow

### Step 1: Retrieve the issue

```
gitlab_get_issue(project_id=<ID>, issue_iid=<NUMBER>)
```

Extract and note:
- **Title** — used to generate the task file slug (English kebab-case summary).
- **Description** — the main body of requirements.
- **Labels** — priority, type, component labels.
- **Assignee** — who's responsible.
- **Milestone** — if set, indicates timeline constraints.
- **State** — open/closed.

### Step 2: Retrieve comments

```
gitlab_list_issue_discussions(project_id=<ID>, issue_iid=<NUMBER>)
```

Scan all discussion threads for:
- **Implementation clues** — technical hints, constraints, or preferences from stakeholders.
- **Knowledge base links** — URLs matching your wiki/knowledge base. Collect these for further research.
- **Linked issue references** — mentions of other issues (`#1234`, `!567`).
- **Decision history** — agreements or reversals documented in comments.
- **Stakeholder constraints** — "must not change X", "needs to work with Y", etc.

### Step 3: Retrieve linked issues

```
gitlab_list_issue_links(project_id=<ID>, issue_iid=<NUMBER>)
```

For each linked issue, note:
- **Link type** — blocks, is blocked by, relates to.
- **Issue title and state** — is the linked work done, in progress, or planned?
- **Relevance** — does the linked issue affect the plan for this ticket?

### Step 4: Compile the summary

Present a structured summary:

```markdown
## Ticket Summary: #<NUMBER> — <TITLE>

**Status:** <state> | **Labels:** <labels> | **Assignee:** <assignee> | **Milestone:** <milestone>

### Description
<cleaned-up description — keep formatting, strip noise>

### Key Points from Comments
- <point 1 — who said what, when>
- <point 2>

### Knowledge Base References Found
- <URL 1> — <context where it was mentioned>
- <URL 2> — <context>

### Linked Issues
- #<N1> (<link type>) — <title> [<state>]
- #<N2> (<link type>) — <title> [<state>]

### Implementation Constraints
- <constraint 1 from comments/description>
- <constraint 2>
```

---

## When invoked from the task-workflow skill

The summary feeds directly into the task file:
- **Title** → task file slug: `YYMMDD_#<number>_<short-slug>.md`
- **Description + comments** → Objective and Problem Analysis sections.
- **Knowledge base links** → targets for knowledge base search.
- **Linked issues** → noted in the task file's constraints or references.
- **Implementation constraints** → task file's Constraints section.

---

## Adapting for other issue trackers

To adapt this skill for a different issue tracker:

1. **Replace the MCP calls** with your tool's API (Jira REST API, Linear GraphQL, GitHub Issues API, etc).
2. **Map the fields** — every tracker has title, description, status, comments, and links. Map them to the same output structure.
3. **Adjust the linked issues step** — different trackers model relationships differently (Jira: issue links, Linear: relations, GitHub: cross-references).
4. **Update prerequisites** — your tool's API token and MCP server config.

The output format (Step 4) should stay the same — it's what the task-workflow expects.

---

## Error Handling

- **Issue not found (404):** Report to user, suggest checking the issue number and project ID.
- **Auth failure:** Stop, tell user to check API token.
- **No comments:** Report "No comments on this issue" — this is normal for simple tickets.
- **No linked issues:** Report "No linked issues" — proceed without.
