# Requirement Change Candidates

This file is a staging artifact. Nothing from it becomes approved contract until the developer explicitly confirms it.

## Raw Intake

- `RC-RAW-1` — <developer language or newly surfaced requirement issue>
	- source: <chat / review / planning / implementation>
	- status: pending-normalization

## Normalized Candidates

- `REQ-<n>` — <normalized candidate summary>
	- source: <raw-intake ids>
	- evidence: <input docs / review findings / code pointers>
	- status: pending-d01

## Evidence Pending

- `EP-1` — <candidate waiting for later research>
	- blocked_by: <missing evidence>
	- next_trigger: <later batched research re-entry>

## Usage Rules

1. Approved items are promoted into `requirements.md` and then removed from this file.
2. Rejected items are removed after their disposition is recorded in the owning phase artifact.
3. Evidence-pending items stay staged here until a later Research re-entry resolves them.
4. Developer approval remains required before anything moves into `requirements.md`.
5. Keep the entries lightweight and evidence-focused so the orchestrator can adapt them to task-specific context.
