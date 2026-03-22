---
name: repo-bootstrap
description: "Bootstrap onboarding for an undocumented or under-documented repo. Scouts the repo structure, runs per-area deep dives (ideally as separate agent sessions to avoid compression), synthesises a repo overview, and optionally seeds component overviews and file-level MDs. Designed for incremental progress — can stop at any phase."
---

# Repo Bootstrap

Bootstrap onboarding documentation for a repo that has little or no onboarding coverage. Produces a high-quality repo overview file and optionally goes deeper into component overviews and file-level MDs.

**Core constraint:** LLM context is limited. A full repo cannot be understood in a single session. This skill breaks the work into bounded phases where each phase operates on a manageable scope and produces a durable artifact. Phases can be run in separate agent sessions to avoid context compression bleeding between areas.

**Design principles:**
- Each phase produces a standalone artifact. You can stop after any phase.
- Area deep-dives are isolated — one area's context does not compress another's.
- Existing cross-repo onboarding (from already-bootstrapped repos) is used as seed context.
- The developer is consulted during deep-dives for intent, direction, and domain knowledge that code alone can't reveal.
- Artifacts compound: the repo overview makes component overviews easier, which make file-level MDs easier.

---

## Prerequisites

- The target repo must be accessible in the workspace.
- The `create_or_update-onboarding-file` skill must be available (for template compliance in later phases).
- The available time tool must be available for timestamps.
- For multi-agent execution: the workspace must support launching sub-agents or the developer runs separate sessions manually.

---

## Inputs

| Input | Required | Description |
|---|---|---|
| `repo` | Yes | The repo name (e.g., `api-service`, `web-client`) |
| `depth` | No | How far to go: `overview-only` (default), `component-overviews`, or `full` (includes file-level MDs for priority areas) |
| `seed-context` | No | Paths to existing onboarding files from adjacent repos that reference this repo. Auto-discovered if not provided. |
| `priority-areas` | No | Areas to prioritise for deeper passes. If omitted, the scout phase identifies them. |

---

## Phase 1 — Scout

**Goal:** Map the repo's top-level structure and identify distinct functional areas.

**Scope:** Broad but shallow. Read structure, not implementation.

**Steps:**

### 1.1 Gather structural signals

Read these (in parallel where possible) without going deep into any single file:

```bash
# Directory structure (2 levels deep)
find <repo-root> -maxdepth 2 -type d | head -100

# Entry points and config
# Look for: README, setup.py/pyproject.toml/package.json/composer.json,
# Dockerfile, docker-compose, Makefile, config files

# Route/endpoint definitions (language-dependent)
# Python: grep for @app.route, @router, urlpatterns, etc.
# PHP: grep for Route::, ->get(, ->post(
# JS/TS: grep for app.get, router.
# Go: grep for http.Handle, mux.Handle

# Module boundaries
# Python: find all __init__.py or top-level .py files in src/
# PHP: find namespace declarations
# Go: find package declarations
# C: find .mak files, header files with module definitions
```

### 1.2 Check existing cross-repo context

Before exploring blind, check what adjacent repos already know about this one:

1. **Read the current (possibly placeholder) overview** at `onboarding/<repo>/overview.md`.
2. **Search bootstrapped repos' onboarding** for references to this repo:
   ```bash
   grep -r "<repo-name>" onboarding/ --include="*.md" -l
   ```
3. **Read the cross-repo sections** of those files. These name specific files, interfaces, event types, and communication paths — use them as seed points for the deep-dive.
4. **Check the glossary** for terms associated with this repo.
5. **Check `docs/`** for domain docs relevant to this repo.

### 1.3 Identify areas

Divide the repo into **functional areas**. An area is a cohesive group of files that:
- Serve a single purpose (e.g., "WebSocket handling", "telemetry processing", "API gateway")
- Can be understood somewhat independently
- Map roughly to what will become component-level overviews

**Output:** A list of areas, each with:
- **Name** — short descriptive label
- **File boundaries** — which directories/files belong to this area
- **Priority** — high/medium/low based on: cross-repo coupling, complexity, and relevance to active work
- **Seed context** — any cross-repo references or docs that mention this area

Write the scout output to: `onboarding/<repo>/bootstrap/scout-report.md`

### 1.4 Developer review

Present the area list to the developer. Ask:
- Are the area boundaries correct? Should any be split or merged?
- Are the priorities right? What areas are you most likely to work on next?
- Is there domain context the scout missed? (e.g., "that module is deprecated", "those two areas are actually tightly coupled")

Update the scout report with corrections.

---

## Phase 2 — Area Deep-Dives

**Goal:** Produce a thorough area report for each identified area.

**Core rule: One fresh session per area.** This prevents context from area A compressing and degrading the analysis of area B. If running in a single session is unavoidable (e.g., small repo, few areas), process areas sequentially and write each report before starting the next.

### 2.1 Session setup

Each area session starts with this context (and nothing else from other areas):

1. **The scout report** — for orientation on where this area fits.
2. **Seed context for this specific area** — cross-repo onboarding sections that reference files in this area.
3. **Relevant docs** — domain documentation for this area's domain.
4. **The overview template** — so the agent knows what level of detail the final output needs.

### 2.2 Deep-dive procedure

For each area, the agent:

1. **Reads code files thoroughly** — not skimming. Every file in the area boundaries.
   - Note: for large areas (50+ files), prioritise by: entry points first, then core logic, then utilities/helpers, then DTOs/types.
2. **Maps the internal structure:**
   - Key classes/functions and their responsibilities
   - Data flow within the area (what calls what, what data goes where)
   - State management (databases, caches, in-memory state)
   - Error handling patterns
3. **Maps external interfaces:**
   - APIs exposed (HTTP endpoints, WebSocket handlers, queue consumers)
   - APIs consumed (calls to other services, database queries)
   - Cross-repo communication (match against seed context from 2.1)
   - Shared types, enums, constants that must stay in sync
4. **Identifies domain concepts:**
   - Domain terms used in the code (check glossary, ask developer if unfamiliar)
   - Business rules embedded in the code
   - Configuration and environment dependencies
5. **Notes invariants and traps:**
   - Ordering constraints, concurrency rules, cache invalidation logic
   - Things that look simplifiable but aren't (the "don't touch this" list)
   - Implicit contracts (e.g., "this function assumes X has already been called")
6. **Consults the developer:**
   - Why does it work this way? (intent behind non-obvious patterns)
   - Is this the target state or transitional? (migration direction)
   - What breaks if this changes? (hidden dependencies)
   - Any context that isn't in the code or docs?
7. **Searches reference docs** for domain specs governing this area.

### 2.3 Write the area report

Write to: `onboarding/<repo>/bootstrap/areas/<area-name>.md`

Area reports are **working documents** — they don't need to conform to the onboarding template. They're intermediate artifacts that feed the synthesis phase. But they should be thorough enough that someone reading only the report (not the code) can understand the area.

**Area report structure:**

```markdown
# <Area Name> — Area Report

**Repo:** <repo>
**Files:** <count>
**Key paths:** <top-level directories/files>
**Generated:** <date>

## Summary
<2-3 sentences: what this area does and why it exists.>

## Internal Structure
<Classes/modules, their responsibilities, how they relate.
Include a diagram if the relationships are complex.>

## Data Flow
<How data moves through this area. Entry points, transformations, outputs.>

## External Interfaces
<APIs exposed, APIs consumed, cross-repo communication paths.
Name specific files, functions, endpoints.>

## Domain Concepts
<Terms, business rules, configuration.>

## Invariants & Traps
<What must hold true. What looks wrong but is right. What would break.>

## Cross-repo Ties
<Specific interfaces to/from other repos. Name files on both sides.
Note sync requirements.>

## Conventions & Patterns
<Recurring patterns in this area. Naming, error handling, testing.>

## Developer Notes
<Anything the developer explained that isn't in the code.
Migration direction, intent, planned changes.>

## Key Files
<Ranked list of the most important files with 1-line descriptions.>
```

---

## Phase 3 — Synthesis

**Goal:** Produce the repo-level overview from the area reports.

**This is a separate session** that reads area reports, not code. The context budget goes to synthesis, not re-reading source files.

### 3.1 Session setup

Load into context:
1. All area reports from `onboarding/<repo>/bootstrap/areas/`
2. The scout report (for the area map and priorities)
3. The existing placeholder overview (if any)
4. Cross-repo onboarding sections from adjacent repos

### 3.2 Compose the repo overview

The overview should include:

1. **What This Repo Is** — purpose, tech stack, deployment model
2. **Architecture at a Glance** — ASCII diagram showing major components and their relationships
3. **Code Structure** — tables mapping areas to paths, tech, and purpose
4. **Functional Areas** — prose summaries of each area (derived from area reports), grouped by domain
5. **Cross-Repo Ties** — tables showing interfaces to each adjacent repo with connection type, nature, and key files on both sides
6. **Build & Dev** — how to build, run, test
7. **Key Invariants** — repo-wide rules that must hold (aggregated from area reports)
8. **Glossary Terms** — terms introduced or heavily used by this repo
9. **Reference Docs** — links to docs relevant to this repo
10. **What to Explore Next** — which component overviews should be written first

Write to: `onboarding/<repo>/overview.md` (replacing the placeholder)

### 3.3 Developer review

Present the overview for review. This is the most important artifact — it sets the frame for everything below it. Get explicit sign-off before proceeding.

### 3.4 Update the onboarding index

Update `onboarding/index.md` to reflect the new status (e.g., Phase 2 → "Bootstrapped — repo overview complete").

---

## Phase 4 — Deepen (optional)

**Goal:** Use the overview + area reports to go deeper — component overviews and file-level MDs.

This phase is unbounded. It can be done incrementally, one component at a time, as needed. It is most valuable when a task is about to touch an area — bootstrap the component overview just-in-time.

### 4.1 Component overviews

For each priority area (now a "component" in onboarding terms):

1. **Re-read the area report** — this is the primary input.
2. **Re-read key source files** if the area report lacks detail on specific patterns.
3. **Write the component overview** at `onboarding/<repo>/<component>/overview.md`.
4. **Include an Onboarding File Index** section listing which file-level MDs exist (initially empty) and which should be created.

### 4.2 File-level MDs

For high-priority files within a component:

1. **Invoke the `create_or_update-onboarding-file` skill** — it handles the template, metadata, and procedures.
2. **Prioritise:** entry points, complex logic, cross-repo interface files, files with non-obvious invariants.
3. **Don't try to cover everything.** File-level MDs for simple utilities, DTOs, or config can wait until a task touches them.

### 4.3 Clean up bootstrap artifacts

Once the component overviews and file-level MDs are written:

- The area reports in `onboarding/<repo>/bootstrap/areas/` become redundant. They can be kept as reference or deleted.
- The scout report can be kept as a historical record of the initial structure assessment.
- **Do not delete bootstrap artifacts while they are still being used** as input for ongoing deepening work.

---

## Multi-Agent Execution Model

The ideal execution uses separate agent sessions to avoid context contamination:

```
Session 1 (Scout):
  → Reads repo structure, cross-repo context
  → Writes scout-report.md
  → Developer reviews and corrects

Session 2..N (Area deep-dives, one per area):
  → Reads: scout-report + seed context for THIS area only
  → Reads: code files in THIS area
  → Consults: docs, developer
  → Writes: areas/<area-name>.md

Session N+1 (Synthesis):
  → Reads: ALL area reports + scout report
  → Does NOT re-read source code
  → Writes: overview.md
  → Developer reviews

Session N+2..M (Deepen, one per component):
  → Reads: overview + relevant area report
  → Reads: code files for THIS component
  → Writes: component overview + file-level MDs
```

**Single-session fallback:** If separate sessions aren't practical (small repo, few areas, developer prefers continuity), run phases sequentially in one session. Write each area report to disk *before* starting the next area to create a checkpoint. Accept that later areas may have less context fidelity due to compression.

---

## When to Use This Skill

| Situation | Use this skill? |
|---|---|
| New repo added to workspace, zero onboarding | Yes — full pipeline |
| Repo has placeholder overview, needs real content | Yes — can skip to Phase 1 scout |
| Task will touch an un-bootstrapped area of a partially covered repo | Yes — run scout + targeted area deep-dive + synthesis update |
| Repo is already well-bootstrapped, just needs a new component overview | No — use `create_or_update-onboarding-file` directly |
| Small script repo with 5 files | Probably not — write the overview directly |

---

## Relationship to Other Skills

| Skill | Relationship |
|---|---|
| `discovery` | This skill uses the same cross-repo discovery techniques during Phase 2 area deep-dives. |
| `create_or_update-onboarding-file` | This skill's Phase 4 invokes it for file-level MDs. The template and conventions defined there apply to all output. |
| `onboarding-drift-detection` | Not used during bootstrap (nothing to drift-check yet). Becomes relevant after bootstrap when code changes. |
| `task-workflow` | The task-workflow can invoke this skill when it discovers that the area it needs to plan against has no onboarding. |
| `brave-search` | May be invoked during area deep-dives for framework/library documentation. |
| `context7-query` | May be invoked during area deep-dives for library API verification. |
