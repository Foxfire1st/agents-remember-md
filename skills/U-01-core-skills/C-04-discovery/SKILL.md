---
name: C-04-discovery
description: "Perform top-down discovery across onboarding, reference docs, cross-repo relationships, and code before acting on unfamiliar surfaces."
---

# C-04 Discovery

Use this retained global utility skill when the current surface is unfamiliar and you need adjacent context before making decisions, asking clarifying questions, or changing code.

Its job is to keep investigation ordered: onboarding first, reference context next, cross-repo relationships after that, and code only once the surrounding meaning is clear enough.

## When To Use It

1. At the start of a task that touches unfamiliar code or architecture.
2. During task planning when the change crosses repo or service boundaries.
3. During implementation when an unfamiliar interface, event, or data shape appears.
4. During direct clarification when code alone does not explain the surrounding system context.

## Top-Down Discovery Order

Always prefer top-down discovery over brute-force code roaming.

1. Read the relevant repo overview under `<onboarding-root>/<repo>/overview.md`.
2. If the area is narrower, read the component overview and any relevant file-level onboarding docs.
3. Read any entity catalog, glossary, or naming reference that helps disambiguate the terms in play.
4. Follow documentation references surfaced by onboarding before broad browsing.
5. Discover cross-repo relationships and system boundaries.
6. Only then read code files directly.

If onboarding is clearly stale or missing for the in-scope surface, do not quietly treat it as trustworthy. Qualify that gap and use `C-02-onboarding-drift-detection` or the appropriate onboarding maintenance flow when needed.

## Discovery Techniques

### From onboarding and docs

1. Follow docs referenced by onboarding first because those pointers usually lead to the intended canonical context.
2. Look for API specs, event catalogs, payload schemas, architectural notes, and diagrams that define system boundaries.
3. Prefer authoritative docs over scattered chat assumptions or local guesses.

### From code

1. Search for API endpoints, base URLs, hostnames, or client wrappers that point to other systems.
2. Search for message names, event types, MQTT topics, websocket channels, or queue subjects.
3. Look for shared type names, entity names, enums, or identifier formats reused across layers.
4. Check configuration and environment wiring for service-to-service relationships.
5. Use code only after the adjacent context is known well enough to interpret what you are reading.

### From adjacent repos or surfaces

1. When you find an outbound call, search the likely receiving surface for the handler.
2. When you find an inbound handler, search the likely sending surface for the producer.
3. Look for equivalent data models or mirrored type definitions across repos.
4. Record naming drift explicitly instead of assuming one local label is canonical.

## Outputs And Handoffs

Discovery itself is not a final artifact owner. Its output is clarified context that should feed the owning surface:

1. Research and Design when deeper investigation is needed
2. `C-01-findings-capture` when durable clarified truth emerges
3. onboarding maintenance when discovery proves onboarding is missing or stale
4. implementation or review when the result is a clarified dependency or interface relationship

## Boundaries

1. Discovery remains a global utility skill, not a phase-local replacement for Research or Design.
2. It does not approve requirements or architecture.
3. It does not replace onboarding drift detection or onboarding maintenance.
4. It should help the caller understand the system before acting, not become a sink for unrelated exploration.
5. This retained package is intentional and should not be treated as accidental drift.

## Rules

1. Start from onboarding and adjacent docs whenever they exist.
2. Make cross-repo discovery explicit instead of assuming boundaries are already documented.
3. When trust in onboarding is uncertain, say so.
4. Route durable findings to their owning artifact instead of leaving them only in chat.
5. Keep the retained-package status explicit even though there is no dedicated onboarding companion for this skill yet.
