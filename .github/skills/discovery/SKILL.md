---
name: discovery
description: "Top-down discovery procedure for understanding code before acting. Covers the onboarding-first reading order and cross-repo relationship discovery techniques. Invoked by task-workflow (Phase 2.1/2.2) and repo-bootstrap (Phase 2)."
---

# Discovery

This skill defines the procedure for understanding code before acting on it. It ensures the agent has adjacent context — onboarding, reference docs, cross-repo interfaces — before making decisions.

**When to invoke:**
- At the start of any task that touches code (task-workflow Phase 2.1).
- During repo bootstrap area deep-dives (repo-bootstrap Phase 2).
- Whenever you encounter unfamiliar code during implementation.

---

## Top-Down Discovery Order

**Always prefer top-down discovery over brute-force code roaming.**

Before diving into code, follow this path:

1. Read the relevant onboarding index or repo overview (`onboarding/<repo>/overview.md`).
2. Check for a component-level overview (`onboarding/<repo>/<component>/overview.md`) and relevant file-level MDs.
3. Check matching `*.instructions.md` files for invariants and cross-repo ties.
4. Consult the glossary for unfamiliar terms.
5. Check `/docs/` for relevant reference documentation (API specs, event catalogs, domain definitions) that provides authoritative system context.
6. **Actively discover cross-repo relationships** using the techniques below.
7. Only then read code files directly.

This ensures the model has adjacent context — including reference documentation and cross-repo interfaces — before making decisions. Do not skip this for the sake of speed.

**If onboarding doesn't exist** for the target repo or area (placeholder overview, no component MDs), flag this to the developer. For task-workflow, invoke `repo-bootstrap` to produce at least a scout report and repo overview before planning. Without onboarding, the plan will miss invariants, cross-repo contracts, and migration direction.

---

## Cross-Repo Discovery

Cross-repo ties do not document themselves. They must be **actively discovered** through code, docs, and adjacent repos — not just read from existing onboarding.

### From code
- Search for **API endpoint URLs, base URLs, or service hostnames** that point to other services.
- Search for **WebSocket event names, message queue topics, or message type strings** — these often originate in one repo and are consumed in another.
- Look for **environment variables** that reference other services (e.g., `API_URL`, `QUEUE_CONNECTION`).
- Search for **shared type names, entity names, or enum values** that appear across repos.
- Look for **HTTP client calls, fetch requests, or SDK invocations** that call external APIs.
- Check **config files** (Docker Compose, infrastructure configs) for service-to-service wiring.

### From reference docs
- Check `/docs/` for **API specifications, interface contracts, and protocol definitions** that describe how systems interact.
- Look for **event catalogs, command definitions, and payload schemas** — these define the shared language between repos.
- Check for **sequence diagrams or data flow descriptions** that show which systems participate in a flow.

### From adjacent repos
- When you find an outbound call in one repo (API call, WebSocket message, queue publish), **search the likely receiving repo** for the handler.
- When you find an inbound handler, **search the likely sending repo** for what triggers it.
- Look for **matching type definitions or equivalent structures** across repos (e.g., a TypeScript interface in one repo and a Python dataclass in another that represent the same entity).

### When to discover
- **During bootstrap:** Systematically, as part of building the onboarding hierarchy.
- **During task planning:** When the task touches code that communicates externally.
- **During implementation:** When you encounter an unfamiliar API call, event, or message format.
- **During drift review:** When changes in one repo may affect contracts consumed by others.

Discovered relationships should be documented in the relevant onboarding file's cross-repo references and, if the term is cross-cutting, added to the glossary.

---

## Relationship to Other Skills

| Skill | Relationship |
|---|---|
| `task-workflow` | Invokes this skill in Phase 2.1 (context gathering) and 2.2 (cross-repo awareness). |
| `repo-bootstrap` | Uses this skill's techniques during Phase 2 area deep-dives. |
| `onboarding-drift-detection` | Should run before discovery to ensure onboarding is current. |
