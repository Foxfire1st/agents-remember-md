---
applyTo: "docs/glossary/**"
---

# Glossary Instructions

## Structure

- `docs/glossary/index.md` — Master glossary. All cross-repo terms MUST be here.
- `docs/glossary/<domain>.md` — Scoped sub-glossaries for terms that only appear within a single subsystem.

## Rules

- Cross-repo terms belong in `index.md`. When in doubt, put it in the index.
- Each entry should define: canonical term, deprecated synonyms, relevant repos, related API/DB/event names, adjacent terms, and links to reference docs.
- The glossary is a navigation and reasoning layer, not just a dictionary.
- When encountering an unfamiliar term during any task, check here first.
- When a new cross-repo term is discovered, add it immediately.
