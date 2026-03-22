---
name: repo-bootstrap
description: "Bootstrap onboarding for an undocumented or under-documented repo. Uses parallel specialized agents to scout structure, deep-dive areas, and synthesise a repo overview. Designed for context efficiency — orchestrator stays thin, agents write to disk, findings carry confidence levels. Can stop after any phase."
---

# Repo Bootstrap

Bootstrap onboarding documentation for a repo that has little or no onboarding coverage. Produces a high-quality repo overview file and optionally goes deeper into component overviews and file-level MDs.

**Core constraint:** LLM context is limited. A full repo cannot be understood in a single session. This skill breaks the work into bounded phases where each phase operates on a manageable scope and produces a durable artifact. Agents write findings to disk — the orchestrator never ingests raw code or structural data, only distilled reports.

**Design principles:**
- **Thin orchestrator.** The orchestrator dispatches agents and reads their distilled outputs. It never loads raw code, directory trees, or grep results into its own context.
- **Parallel specialization.** Where possible, multiple narrow-focus agents analyse the same scope simultaneously, each with a fresh context window dedicated to its lens.
- **Confidence-tagged findings.** Every factual claim in a report carries a confidence level so downstream consumers know what's verified vs. inferred.
- **Goal-backward phases.** Each phase defines observable "done when" conditions, not just steps to perform.
- **Durable checkpoints.** Each phase produces a standalone artifact. You can stop after any phase. A state file tracks progress across sessions.
- **Cross-repo awareness.** Existing onboarding from adjacent repos is used as seed context. When no onboarding exists, the scout falls back to direct code scanning.
- **Developer consultation.** The developer is consulted at review gates for intent, direction, and domain knowledge that code alone can't reveal.
- **Artifacts compound.** The scout report feeds deep-dives, deep-dives feed synthesis, synthesis feeds component overviews.

---

## Prerequisites

- The target repo must be accessible in the workspace.
- The `create_or_update-onboarding-file` skill must be available (for template compliance in later phases).
- The `discovery` skill must be available (for cross-repo discovery techniques).
- The available time tool must be available for timestamps.
- For parallel agent execution: the workspace must support launching sub-agents, or the developer runs separate sessions manually.

---

## Inputs

| Input | Required | Description |
|---|---|---|
| `repo` | Yes | The repo name (e.g., `api-service`, `web-client`) |
| `depth` | No | How far to go: `overview-only` (default), `component-overviews`, or `full` (includes file-level MDs for priority areas) |
| `seed-context` | No | Paths to existing onboarding files from adjacent repos that reference this repo. Auto-discovered if not provided. |
| `priority-areas` | No | Areas to prioritise for deeper passes. If omitted, the scout phase identifies them. |

---

## Bootstrap State File

Every bootstrap produces a state file at `onboarding/<repo>/bootstrap/STATE.md`. This is the first file read at the start of every session and the last file updated at the end.

**Purpose:** Track progress, decisions, and open questions across sessions. When a bootstrap spans multiple sessions (the recommended approach), the state file is the thread that ties them together.

**Template:**

```markdown
# Bootstrap State — <repo>

**Started:** <date>
**Current phase:** <phase and substep>
**Last updated:** <date>

## Areas

| Area | Priority | Scout | Deep-dive | Brief | Merged | Synthesis |
|---|---|---|---|---|---|---|
| <area> | high | done | done | done | done | done |
| <area> | high | done | in-progress | — | — | — |
| <area> | medium | done | — | — | — | — |

## Decisions

| # | Date | Decision | Context |
|---|---|---|---|
| 1 | <date> | <what was decided> | <why — who said what> |

## Parking Lot

- <questions, observations, and uncertainties for future sessions>
```

**Rules:**
- Created during Phase 1 (scout), updated at every phase transition.
- Read at the start of every session — before the scout report, before area reports.
- Tracks decisions so they don't need to be re-asked across sessions.
- "Parking lot" captures questions and observations that can't be resolved in the current session.
- Area table columns track progress through the pipeline: scout → deep-dive → brief → merged → synthesis.

---

## Confidence Levels

All factual claims in scout reports, area reports, and area briefs carry a confidence tag:

- **`[HIGH]`** — Confirmed by developer, documented in reference docs, or verified across repos (same interface matched on both sides).
- **`[MEDIUM]`** — Clear from code reading, follows established patterns, but not externally confirmed.
- **`[LOW]`** — Inferred, speculative, or based on indirect evidence (naming, comments, partial patterns).

**Rules:**
- Default to `[MEDIUM]` for code-derived findings. Only promote to `[HIGH]` with external confirmation.
- During synthesis: `[LOW]` findings are flagged as "Needs verification" rather than stated as fact.
- During developer review: the developer can promote or dismiss findings at any level.
- The confidence tag appears inline next to the claim: `[HIGH] Orders are dispatched via the event bus to the fulfillment service`.

---

## Phase 1 — Scout

**Goal:** Map the repo's top-level structure, tech stack, and cross-repo interfaces. Identify distinct functional areas.

**Scope:** Broad but shallow. Read structure and configuration, not implementation.

**Execution model:** The scout runs as a **sub-agent** to keep the orchestrator's context clean. The orchestrator dispatches the scout with the repo path and any known seed context. The scout writes its report to disk. The orchestrator receives only a confirmation and reads the finished report.

> **Fallback for tiny repos (< 10 files):** The orchestrator can run the scout inline — the overhead of a sub-agent exceeds the context savings.

**Done when:**
- Every top-level directory is assigned to exactly one area (or explicitly marked as out-of-scope, e.g., `node_modules`, `bin`).
- Every area has file boundaries, a priority, tech profile, and seed context.
- A cross-repo interface map exists (even if empty, with reasoning for why no ties were found).
- The developer has confirmed the area map.

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

### 1.2 Build tech profile

Catalog the technology in use so deep-dive agents know what they're looking at:

1. **Read package manifests** — `package.json`, `*.csproj`, `pyproject.toml`, `composer.json`, `go.mod`, `Cargo.toml`, etc.
2. **Identify:** languages (with rough % split), frameworks, key libraries and their versions.
3. **Note infrastructure patterns** — Dockerfile, docker-compose, CI/CD config, cloud platform references.
4. **Map tech to areas** where possible — "queue area uses RabbitMQ client 3.x", "API area uses Express + Prisma".
5. **Identify build system** — npm/yarn/dotnet/make/cargo/etc., and build commands.

The tech profile becomes a section in the scout report. Deep-dive agents receive it as seed context.

### 1.3 Cross-repo discovery

Before exploring blind, discover what's known about this repo's external interfaces. Use two paths depending on whether adjacent repos have onboarding.

#### Path A — Onboarding exists in adjacent repos

1. **Read the current (possibly placeholder) overview** at `onboarding/<repo>/overview.md`.
2. **Search bootstrapped repos' onboarding** for references to this repo:
   ```bash
   grep -r "<repo-name>" onboarding/ --include="*.md" -l
   ```
3. **Read the cross-repo sections** of those files. These name specific files, interfaces, event types, and communication paths — use them as seed points for the deep-dive.
4. **Check the glossary** for terms associated with this repo.
5. **Check `docs/`** for domain docs relevant to this repo.

#### Path B — No onboarding available (first bootstrap in workspace)

When adjacent repos have little or no onboarding, go directly to code:

1. **Scan the target repo for outbound signals:**
   - HTTP client calls, base URLs, service hostnames in config/environment variables
   - Message queue publish calls, WebSocket connection URLs, gRPC client stubs
   - Shared type imports or cross-repo package references
   - Environment variables referencing other services (e.g., `AUTH_SERVICE_URL`, `QUEUE_HOST`)

2. **For each outbound signal, search the likely receiving repo:**
   - Match endpoint URLs to route definitions
   - Match queue topics/channels to subscription handlers
   - Match event/message type names across repos

3. **Scan adjacent repos for inbound references to the target repo:**
   - Same technique in reverse — look for URLs, topics, or type names that point here

4. **Produce a cross-repo interface map** with confidence levels:
   - `[HIGH]` — matched on both sides (outbound call in repo A, handler in repo B)
   - `[MEDIUM]` — found outbound signal, likely target repo identified but handler not confirmed
   - `[LOW]` — env var or config reference suggests a tie, but no code-level confirmation

Use the techniques described in the `discovery` skill's Cross-Repo Discovery section for systematic coverage.

### 1.4 Identify areas

Divide the repo into **functional areas**. An area is a cohesive group of files that:
- Serve a single purpose (e.g., "WebSocket handling", "queue processing", "API gateway")
- Can be understood somewhat independently
- Map roughly to what will become component-level overviews

**Output per area:**
- **Name** — short descriptive label
- **File boundaries** — which directories/files belong to this area
- **File count** — for sizing the deep-dive (determines whether parallel agents are warranted)
- **Priority** — high/medium/low based on: cross-repo coupling, complexity, and relevance to active work
- **Tech** — frameworks/libraries specific to this area (from the tech profile)
- **Seed context** — any cross-repo references or docs that mention this area
- **Initial observations** — 1-2 sentences on what this area appears to do

### 1.5 Write the scout report

Write to: `onboarding/<repo>/bootstrap/scout-report.md`

**Scout report template:**

```markdown
# Scout Report — <repo>

**Generated:** <date>
**Repo root:** <path>
**Total files:** <count>
**Total directories:** <count>

## Tech Profile

| Technology | Version | Areas | Notes |
|---|---|---|---|
| <framework> | <version> | <areas> | <role in the system> |

**Languages:** <list with rough % split>
**Infrastructure:** <Docker, CI/CD, cloud platform>
**Build system:** <tool and key commands>

## Areas

### <Area Name>
- **Files:** <count>
- **Boundaries:** <directories/file patterns>
- **Priority:** <high/medium/low> — <reason>
- **Tech:** <frameworks/libraries specific to this area>
- **Seed context:** <cross-repo refs, docs, glossary terms>
- **Initial observations:** <1-2 sentences>

### <Area Name>
...

## Cross-Repo Interface Map

| Direction | This Repo (file/function) | Other Repo | Interface Type | Confidence |
|---|---|---|---|---|
| outbound | <source location> | <target repo> | <HTTP/WS/queue/gRPC/etc.>: <detail> | [HIGH/MEDIUM/LOW] |
| inbound | <handler location> | <source repo> | <type>: <detail> | [HIGH/MEDIUM/LOW] |

## Unresolved

- <things the scout couldn't determine — feeds developer review and parking lot>

## Developer Review Questions

- Are the area boundaries correct? Should any be split or merged?
- Are the priorities right? What areas are you most likely to work on next?
- Is there domain context the scout missed?
- <specific questions based on unresolved items>
```

### 1.6 Developer review

Present the area list and cross-repo interface map to the developer. Ask the questions from the scout report's "Developer Review Questions" section. Also ask:
- Is there domain context the scout missed? (e.g., "that module is deprecated", "those two areas are actually tightly coupled")
- For `[LOW]` confidence cross-repo ties: can you confirm or dismiss these?

Update the scout report with corrections. Create the bootstrap state file.

---

## Phase 2 — Area Deep-Dives

**Goal:** Produce a thorough, confidence-tagged area report for each identified area.

**Done when:** For each area, a reader of the area report (who has not read the code) can answer: What does this area do? How does data flow through it? What external systems does it talk to? What would break if you changed it carelessly? Every factual claim carries a confidence tag.

### 2.1 Execution model — parallel specialized agents

For each area, spawn up to **4 focused sub-agents** in parallel. Each agent gets a fresh context window dedicated to its lens.

| Agent | Focus | Output file |
|---|---|---|
| **Structure agent** | Internal architecture: classes, modules, data flow, state management | `areas/<area>/structure.md` |
| **Interface agent** | APIs exposed/consumed, cross-repo ties, shared types, communication protocols | `areas/<area>/interfaces.md` |
| **Pattern agent** | Conventions, error handling, testing patterns, naming, code style, domain concepts | `areas/<area>/patterns.md` |
| **Concerns agent** | Invariants, traps, tech debt, fragile code, concurrency, implicit contracts | `areas/<area>/concerns.md` |

**Each sub-agent receives:**
1. The scout report (for orientation and tech profile).
2. Seed context for this specific area — cross-repo onboarding sections, reference docs.
3. The file list for this area (from the scout report's area boundaries).
4. The confidence level definitions (from this skill).
5. Instructions to write findings to its designated output file with confidence tags.

**Each sub-agent does NOT receive:**
- Content from other areas.
- Output from other sub-agents (they run in parallel).
- The full repo file listing beyond this area.

**The orchestrator never reads the section files.** It only receives confirmation that they were written.

> **Fallback for small areas (< 15 files):** Skip parallelization. Run a single agent that covers all four lenses sequentially and writes a unified area report directly. The overhead of 4 agents isn't justified for a small scope.

### 2.2 Sub-agent procedures

#### Structure agent

1. Read all code files in the area boundaries (for large areas 50+ files: entry points first, then core logic, then utilities, then DTOs/types).
2. Map key classes/functions and their responsibilities.
3. Trace data flow: what calls what, what data goes where.
4. Identify state management: databases, caches, in-memory state.
5. Produce a structure diagram if relationships are complex.

#### Interface agent

1. Map APIs exposed: HTTP endpoints, WebSocket handlers, queue consumers, gRPC services.
2. Map APIs consumed: calls to other services, database queries, external API clients.
3. Match cross-repo communication against seed context from the scout report.
4. Identify shared types, enums, constants that must stay in sync across repos.
5. Search reference docs for protocol specs and API contracts.

#### Pattern agent

1. Identify coding conventions: naming, file organization, module patterns.
2. Document error handling patterns: how errors are caught, propagated, logged.
3. Identify testing patterns: test framework, coverage approach, fixtures.
4. Extract domain concepts: terms, business rules, configuration dependencies.
5. Check the glossary for unfamiliar terms; flag new terms for glossary addition.

#### Concerns agent

1. Identify invariants: ordering constraints, concurrency rules, cache invalidation logic.
2. Flag traps: things that look simplifiable but aren't, implicit contracts.
3. Note tech debt: TODOs, workarounds, deprecated patterns, version pinning.
4. Identify fragile code: areas where small changes could have outsized impact.
5. Note security-relevant patterns: auth checks, input validation, secret handling.

### 2.3 Merge step

After all sub-agents complete for an area, a **merge agent** (fresh context) composes the unified area report:

1. Read the 4 section files for this area.
2. Cross-reference findings — interfaces mentioned by the interface agent should appear in the structure agent's data flow.
3. Resolve conflicts — if agents disagree on a finding, note both perspectives and tag as `[LOW]` until resolved.
4. Consult the developer for anything tagged `[LOW]` or flagged as uncertain.
5. Write the unified area report.

Write to: `onboarding/<repo>/bootstrap/areas/<area-name>.md`

**Area report template:**

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
Include a diagram if the relationships are complex.
Tag claims: [HIGH]/[MEDIUM]/[LOW].>

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

## Unverified / Low Confidence
<Collected [LOW] findings from all agents. These need developer input or deeper investigation.>
```

### 2.4 Write the area brief

After the merged area report is complete, produce a **1-page area brief** — a distilled summary for the synthesis phase:

Write to: `onboarding/<repo>/bootstrap/areas/<area-name>.brief.md`

```markdown
# <Area Name> — Brief

**Files:** <count> | **Priority:** <high/medium/low> | **Generated:** <date>

## What it does
<2-3 sentences.>

## Key interfaces
<Bulleted list: APIs exposed, APIs consumed, cross-repo ties. Name files.>

## Top invariants
<Bulleted list: the 3-5 most important things that must hold true.>

## Cross-repo ties
<Bulleted list: interfaces to/from other repos with confidence tags.>

## Unresolved
<Anything tagged [LOW] or flagged for developer input.>
```

The brief exists so the synthesis phase can load lightweight summaries instead of full area reports.

### 2.5 Update state

After each area completes its deep-dive cycle (section files → merge → brief):
1. Update `STATE.md` — mark the area's deep-dive, brief, and merged columns.
2. Add any decisions made during developer consultation.
3. Move resolved parking lot items; add new ones.

---

## Phase 3 — Synthesis

**Goal:** Produce the repo-level overview from the area briefs and scout report.

**This is a separate session** that reads distilled artifacts, not code or full area reports. The context budget goes to composing, not loading.

**Done when:** A new developer reading only the repo overview can orient themselves in the codebase, identify which area to look at for a given task, understand the cross-repo boundaries, and know what technology is in use. All `[LOW]`-confidence items are either resolved or explicitly flagged.

### 3.1 Session setup

Load into context:
1. **Area briefs** from `onboarding/<repo>/bootstrap/areas/*.brief.md` — not full reports.
2. **The scout report** — for the area map, tech profile, and cross-repo interface map.
3. **The bootstrap state file** — for decisions and unresolved items.
4. **The existing placeholder overview** (if any).
5. **An existing high-quality repo overview** (if available in the workspace) as a quality reference.
6. **Cross-repo onboarding sections** from adjacent repos.

> If the synthesis agent needs detail beyond what a brief provides, it reads that one full area report on demand. It does not load all full reports upfront.

### 3.2 Compose the repo overview

The overview should include:

1. **What This Repo Is** — purpose, tech stack, deployment model
2. **Architecture at a Glance** — ASCII diagram showing major components and their relationships
3. **Code Structure** — tables mapping areas to paths, tech, and purpose
4. **Functional Areas** — prose summaries of each area (derived from area briefs), grouped by domain
5. **Cross-Repo Ties** — tables showing interfaces to each adjacent repo with connection type, nature, and key files on both sides
6. **Build & Dev** — how to build, run, test
7. **Key Invariants** — repo-wide rules that must hold (aggregated from area briefs)
8. **Glossary Terms** — terms introduced or heavily used by this repo
9. **Reference Docs** — links to docs relevant to this repo
10. **What to Explore Next** — which component overviews should be written first

**Confidence handling in the overview:**
- `[HIGH]` and `[MEDIUM]` findings are stated as facts.
- `[LOW]` findings are either omitted or placed in a "Needs Verification" callout, not stated as facts.
- If many `[LOW]` items remain, flag this to the developer — it may indicate the area needs a targeted deep-dive with more developer consultation.

Write to: `onboarding/<repo>/overview.md` (replacing the placeholder)

### 3.3 Developer review

Present the overview for review. This is the most important artifact — it sets the frame for everything below it. Get explicit sign-off before proceeding.

During review, ask the developer to:
- Confirm or dismiss any remaining `[LOW]`-confidence items.
- Verify the cross-repo tie table is complete.
- Flag any areas where the summary doesn't match their understanding.

### 3.4 Update the onboarding index and state

1. Update `onboarding/index.md` to reflect the new status (e.g., "Bootstrapped — repo overview complete").
2. Update `STATE.md` — mark synthesis complete, record any final decisions from the review.

---

## Phase 4 — Deepen (optional)

**Goal:** Use the overview + area reports to go deeper — component overviews and file-level MDs.

This phase is unbounded. It can be done incrementally, one component at a time, as needed. It is most valuable when a task is about to touch an area — bootstrap the component overview just-in-time.

**Done when:** For each component addressed: a developer about to work in that component can read the component overview and understand the internal structure, key files, patterns, and pitfalls without reading every source file first. File-level MDs exist for the highest-risk files (those with invariants, cross-repo interfaces, or non-obvious logic).

### 4.0 Priority re-assessment

Before deepening, re-assess which components to tackle first. Priorities may have shifted since the scout phase:

1. **Read the repo overview** — synthesis may have revealed that a medium-priority area has critical cross-repo ties.
2. **Read `STATE.md`** — decisions and parking lot items may point to areas that need attention.
3. **Check for just-in-time triggers** — if this phase was invoked by `task-workflow` because a task is about to touch a specific area, that area takes priority regardless of the scout's original ranking.
4. **Produce a deepening order** — a ranked list of components to address. Update `STATE.md` with this list.

### 4.1 Component overviews

Each component overview runs as a **sub-agent** to keep the orchestrator clean for managing the sequence across components.

**Sub-agent receives:**
1. **The repo overview** — for context on where this component fits in the whole.
2. **The full area report** for this component (not just the brief — the brief was for synthesis, the full report has the detail needed here).
3. **The Phase 2 section files** for this component — especially `concerns.md` (invariants, traps, fragile code) and `interfaces.md` (cross-repo ties, shared types). These are more detailed than the merged report for their specific lenses.
4. **The scout report's tech profile** — so the overview accurately describes the component's tech stack.
5. **The component's source files** — for re-reading key files where the area report lacks detail.

**Sub-agent does NOT receive:**
- Area reports, section files, or source code from other components.
- The full scout report (only the tech profile section).

**Sub-agent procedure:**

1. **Read the full area report and section files** — these are the primary inputs. Note any `[LOW]`-confidence findings that need resolution.
2. **Re-read key source files** where the area report lacks detail on specific patterns, or where the concerns agent flagged traps/invariants that need more context to document clearly.
3. **Write the component overview** at `onboarding/<repo>/<component>/overview.md` following the established convention.
4. **Include an Onboarding File Index** section listing:
   - Which file-level MDs should be created (ranked by priority — see 4.3 for criteria).
   - Which file-level MDs exist (initially empty for a fresh bootstrap).
5. **Propagate confidence tags** — claims carried from the area report retain their confidence level. New claims from re-reading code default to `[MEDIUM]`.

### 4.2 Developer review

After each component overview is written, present it to the developer for review. Ask:
- Does the overview match your understanding of this component?
- Are there invariants or traps missing that you know about?
- For `[LOW]`-confidence items: can you confirm or dismiss?
- Is the file-level MD priority ranking correct?

Update the component overview with corrections. Update `STATE.md`.

> This gate prevents misunderstandings from propagating into file-level MDs. A wrong component overview produces wrong file-level docs.

### 4.3 File-level MDs

For high-priority files within an approved component:

1. **Invoke the `create_or_update-onboarding-file` skill** — it handles the template, metadata, and procedures.
2. **Prioritise using Phase 2 artifacts** — the concerns agent's output directly identifies which files need documentation most:
   - Files flagged in `concerns.md` as having **invariants or implicit contracts** — these are the "landmine" files where a developer could easily break things without documentation.
   - Files flagged in `interfaces.md` as **cross-repo interface points** — changes here can break other repos.
   - **Entry points** and files with **complex, non-obvious logic** identified in `structure.md`.
   - Files where the area report contains `[LOW]`-confidence claims — documentation forces investigation and resolution.
3. **Don't try to cover everything.** File-level MDs for simple utilities, DTOs, or config can wait until a task touches them.

### 4.4 Update state and clean up

After a component's overview is reviewed and its file-level MDs are written:

1. **Update `STATE.md`** — mark the component as deepened.
2. **Clean up per-component:**
   - The section files for this component (`areas/<area>/structure.md`, `interfaces.md`, `patterns.md`, `concerns.md`) can now be deleted — their content is captured in the component overview and file-level MDs.
   - The area brief for this component can be deleted — it was only needed for synthesis.
   - The full area report can be kept as reference or deleted — it's largely superseded by the component overview but may still be useful for historical context.
3. **Do not delete section files for components that haven't been deepened yet** — they're still needed as input.
4. **The scout report and state file are kept** as historical records throughout.

---

## Multi-Agent Execution Model

The ideal execution uses sub-agents to preserve context. The orchestrator stays thin throughout.

```
Orchestrator:
  → Dispatches all agents
  → Reads only: STATE.md, scout-report.md, area briefs, repo overview
  → Never reads: raw code, directory trees, full area reports, section files

Scout sub-agent (Phase 1):
  → Reads: repo structure, config files, cross-repo code/onboarding
  → Writes: scout-report.md
  → Returns to orchestrator: confirmation only
  → Developer reviews and corrects

Per-area deep-dive (Phase 2 — 4 parallel sub-agents per area):
  → Each reads: scout report + seed context + code files in THIS area
  → Each writes: areas/<area>/<lens>.md (structure/interfaces/patterns/concerns)
  → Returns to orchestrator: confirmation only

Per-area merge agent (Phase 2):
  → Reads: the 4 section files for THIS area
  → Writes: areas/<area-name>.md (merged report) + areas/<area-name>.brief.md
  → Consults: developer for [LOW]-confidence items
  → Returns to orchestrator: confirmation only

Synthesis agent (Phase 3):
  → Reads: area briefs + scout report + STATE.md + cross-repo context
  → Reads on demand: specific full area reports if briefs lack detail
  → Writes: overview.md
  → Developer reviews

Per-component deepen agent (Phase 4 — one per component):
  → Reads: repo overview + full area report + section files (concerns, interfaces)
  →         + scout tech profile + component source files
  → Writes: onboarding/<repo>/<component>/overview.md
  → Returns to orchestrator: confirmation only
  → Developer reviews before file-level MDs proceed
```

**State file touchpoints:**
- Created during Phase 1 after developer review.
- Read at the start of every session.
- Updated after each area completes its deep-dive cycle.
- Updated after synthesis and developer review.
- Updated after each component is deepened and reviewed.
- Remains as historical record after bootstrap completes.

**Single-session fallback:** If separate sessions aren't practical (small repo, few areas, developer prefers continuity):
- Run the scout inline (skip sub-agent for the scout).
- Process areas sequentially. For each area, run the 4 sub-agents (or a single agent for small areas), merge, and write the brief before starting the next area.
- Write each artifact to disk as a checkpoint. Accept that later areas may have less context fidelity due to compression.
- The state file is still maintained — it serves as a recovery point if the session is interrupted.

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
| `discovery` | This skill uses the same cross-repo discovery techniques during Phase 1 (scout) and Phase 2 (area deep-dives). The discovery skill's Cross-Repo Discovery section is the reference for systematic interface scanning. |
| `create_or_update-onboarding-file` | This skill's Phase 4 invokes it for file-level MDs. The template and conventions defined there apply to all output. |
| `onboarding-drift-detection` | Not used during bootstrap (nothing to drift-check yet). Becomes relevant after bootstrap when code changes. |
| `task-workflow` | The task-workflow can invoke this skill when it discovers that the area it needs to plan against has no onboarding. |
| `brave-search` | May be invoked during area deep-dives for framework/library documentation. |
| `context7-query` | May be invoked during area deep-dives for library API verification. |
