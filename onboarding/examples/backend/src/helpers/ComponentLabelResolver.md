# ComponentLabelResolver.php

| Field                  | Value                                    |
| ---------------------- | ---------------------------------------- |
| repository             | `example-application`                    |
| path                   | `src/helpers/ComponentLabelResolver.php` |
| doc_type               | `file-level-onboarding`                  |
| lastUpdated            | 2026-04-23T14:05                         |
| lastVerifiedCommitHash | `example-public-sanitized`               |
| lastVerifiedCommitDate | 2026-04-23T14:05:00Z                     |

## Purpose

`ComponentLabelResolver` centralizes backend display-label rules for ports and modular components. It resolves user-facing labels from index metadata, configuration, and component-category context.

This file is a sanitized public example derived from a private onboarding pattern. Product names, identifiers, and domain terms are intentionally generic.

## Code Commentary

### Logic

- `defaultPortLabel()` and related helpers determine how labels should fall back when configuration data is absent.
- Index-based lookup helpers resolve the matching port or component entry from configuration data.
- Plug-in module components do not rely only on a suffix from identifiers such as `Slot1`; they first check root-device ports on the generated topology, then walk only attached expansions and match the concrete expansion type to the component kind. This traversal is required because the generated topology returns the root device as the top-level object, and querying ports on that root only exposes root-device ports; it does not aggregate ports from attached expansions. A small helper owns the repeated "device type matches component kind plus matching port by identifier" logic so the root-plus-expansions traversal stays explicit without duplicating the inner lookup branch.
- `componentLabel()` provides a consistent display-name path for summary cards and API payloads.

### Invariants

- Naming should stay deterministic for the same configuration and index inputs.
- Fallback labels are important because not every deployment carries complete human-authored names.
- For modular-component naming, topology metadata is authoritative when available; identifier parsing is only a fallback.
- For expansion-backed modules, the topology root is not enough on its own; expansion ports must be consulted explicitly.

### Conventions

- Keep port and component labeling policy here instead of re-encoding it in controllers, presenters, or templates.

## Update History

- 2026-04-23 — Added a sanitized public example that preserves the onboarding structure without exposing private domain vocabulary.
- 2026-04-23 — Documented the explicit root-plus-expansions traversal pattern for expansion-backed module labels.
- 2026-04-23 — Captured the helper-extraction rationale for the repeated component-kind-plus-port match logic.
