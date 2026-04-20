# Input Documentation Workflow

Use this workflow whenever the project-documentation skill needs to create, refresh, expand, or reconcile the task-local input-project-documentation tree.

Its purpose is to build and maintain the task-local input-project-documentation tree as the exact current-state impact map for the approved project slice.

This workflow does not update durable onboarding. It consumes onboarding, technical documentation, and code as evidence and produces task-local input documentation artifacts.

## Goal

Use the currently approved task dossier to discover or refresh the exact existing-file slice affected by the task, then, depending on the caller-selected mode, either build the mapped file-level current-state slice or synthesize the later area overview layer under the task root.

The result should be a task-local current-state documentation tree that later phases can use instead of packet-style current-state artifacts.

## Caller Responsibilities

The invoking phase workflow owns the full dispatch contract.

This reusable workflow should not redefine that contract in file-local detail. The caller is responsible for assembling the prompt, naming the files to read, setting the read order, defining why the invocation is happening, stating which downstream artifact or phase needs the refreshed current-state map next, and specifying which invocation mode is required.

## Invocation Modes

The caller must select one of these modes for each invocation:

### 1. `file-mapping`

Use this mode when the workflow needs to discover the in-scope existing-file slice and create, update, or remove file-level mapped input docs.

This mode:

- creates or updates mirrored file-level input docs only
- may use internal partitioning if the assigned slice is too large for one context window
- must not create overview docs

### 2. `overview-generation`

Use this mode only after the relevant file-level mapped docs are current enough and any required reconciliation for the area has already completed.

This mode:

- creates, updates, or removes one root-level area overview file per area
- synthesizes from existing file-level mapped docs, findings, and reconciliation results
- must not be used as a substitute for missing file-level mapping

## Output Root

Write all primary artifacts under:

```text
<task-folder>/input-project-documentation/
```

Working structure:

```text
<task-folder>/input-project-documentation/
  <area-name>.overview.md          # optional area overview, generated only in overview-generation mode
  <repo>/
    <path>/<to>/<File>.md          # mapped input-documentation file
```

Use these templates:

- `input-documentation-template.md` for file-level docs
- `input-documentation-overview-template.md` for overview docs

## Core Rules

- Input documentation maps existing files only. Do not create file-level input docs for future files.
- Use strict mirrored file mapping for the approved slice. One mapped input doc corresponds to one existing source file.
- Use onboarding, code, docs, and canonical references together seam by seam. Do not batch all onboarding first and all code later.
- Relevant onboarding is evidence, not optional flavor. Use it, qualify it, or explicitly justify why it is not being used.
- Unchanged-but-load-bearing files still belong in the mapped input tree. Mark them clearly and document why they remain constraints.
- Input documentation is task-local. Do not edit or create durable onboarding from this workflow.
- Overview docs are area-level syntheses, not replacements for file-level docs.
- File-mapping mode must not create overview docs.
- Overview-generation mode creates root-level `<area-name>.overview.md` artifacts only after the mapped file docs and any required reconciliation for that area are ready.
- Remove mapped artifacts only when they no longer belong to the currently approved slice. If scope is uncertain and not yet approved, keep the artifact visible and note the uncertainty instead of deleting it silently.

## Responsibilities

This workflow is responsible for:

- discovering which existing files belong in the approved slice
- creating missing mapped input file docs in `file-mapping` mode
- updating existing mapped input file docs as research deepens or corrects the understanding of the slice
- creating or updating root-level `<area-name>.overview.md` docs in `overview-generation` mode
- removing file-level or overview-level input documentation artifacts that no longer belong in the approved slice
- preserving explicit visibility of unchanged-but-load-bearing files

## Procedure

### 1. Read the active task dossier

Read the task-local dossier passed by the caller first, in the order the caller provided.

Establish:

- the approved task boundary
- the current-state question for this invocation
- the areas and seams that need investigation or refresh
- which onboarding is current, newly created at the boundary, deferred, contradicted, or missing
- whether this invocation is creating the initial map or correcting an existing one

### 2. Discover the existing-file slice seam by seam

Run this step in `file-mapping` mode.

Investigate the relevant areas seam by seam rather than area-by-area in bulk.

For each concrete surface:

1. Read the relevant onboarding.
2. Read the corresponding code.
3. Read any already-surfaced canonical docs or reference docs.
4. Compare them immediately while the surface is in view.
5. Identify whether the surface belongs in the approved input slice.

Keep a living in-scope inventory of existing files.

### 3. Create or update mapped input file docs

Run this step in `file-mapping` mode.

For each existing file that belongs in the approved slice:

- create the mapped input doc if it does not exist yet
- update the mapped input doc if it already exists
- use `input-documentation-template.md`

Every file-level input doc should make clear:

- why the file is in scope
- what current logic matters for the task
- which requirements press on it
- what is likely to change around it
- what could break if the task is handled naively
- what evidence supports the assessment

If the file is unchanged but still load-bearing:

- keep the file mapped
- mark the likely change surface as effectively `no direct code change`
- document the invariants, boundaries, and constraints that still matter

### 4. Remove file-level docs that no longer belong in the approved slice

Run this step in `file-mapping` mode.

If a mapped input file doc was created earlier but no longer belongs to the approved slice:

- remove it from `input-project-documentation/` if the scope correction is already part of the approved task boundary
- otherwise, keep it visible until the scope correction is approved and note the uncertainty in the relevant artifact

Do not silently delete artifacts when the approved boundary is still unclear.

### 5. Create or update area overview docs

Run this step only in `overview-generation` mode.

After the file-level input docs are current enough and any required reconciliation for the area is complete, create or update one area overview file at:

```text
<task-folder>/input-project-documentation/<area-name>.overview.md
```

Create or update an overview when:

- the brief-defined area has dense cross-file change pressure
- ownership boundaries are difficult to explain file-by-file
- migration or breakage seams are cross-file by nature
- a single area-level synthesis artifact will help later phases consume the mapped slice without rereading every file doc immediately

Use `input-documentation-overview-template.md`.

Overview docs should:

- start with explicit included-file coverage
- list unchanged-but-load-bearing files first
- synthesize current flow, invariants, authority boundaries, requirement pressure, breakage seams, and scope edges for the area
- avoid duplicating file-level commentary unless the cross-file relationship is the real point

### 6. Remove area overview docs that are no longer justified

If an area overview no longer belongs to the approved slice:

- remove it if the changed scope is already approved
- otherwise keep it visible until the boundary is resolved and note the uncertainty

### 7. Produce or refresh downstream bridge artifacts

When the mapped input tree is current enough for downstream use:

- treat `<task-folder>/input-project-documentation/` as the primary exact current-state artifact set
- point downstream phases to that tree instead of packet-style exact-current-state artifacts
- if the invoking workflow still expects a summary artifact, keep that summary secondary and make it point to the input-project-documentation tree rather than trying to restate it in packet form

## Execution Model

The invoking workflow should not run this procedure inline. It should dispatch the appropriate role agent for the current phase, provide the complete prompt up front, and require that agent to use the project-documentation skill.

The dispatched agent for a given invocation owns:

- reading the dossier for its assigned slice
- following the caller-selected mode exactly
- deciding whether an assigned slice in `file-mapping` mode is small enough for one sequential pass or still needs internal hotspot partitioning
- producing or updating whatever downstream artifact the invoking workflow requires for that assigned slice and mode

For small scopes in `file-mapping` mode, a single sequential pass may be enough.

For larger assigned slices in `file-mapping` mode, a slice may still need to be partitioned into viable sub-surface areas:

- split the assigned slice into non-overlapping hotspots or sub-surfaces
- use one deep-read pass per hotspot or sub-surface
- run those passes in parallel when that helps keep context clean
- have each pass write durable findings for its assigned surface under `<task-folder>/research/findings/<area-name>/<report>.md` before any later reconciliation pass
- document the partition boundaries and passed context into `<task-folder>/research/findings/<area-name>/partitioning_log.md`
- stop after file-level mapped docs, findings, and partition artifacts are written if the caller plans later reconciliation or overview-generation waves

In `overview-generation` mode, the dispatched agent should not redo broad seam discovery. It should read the current mapped file docs, the relevant findings, and any reconciliation artifacts for the assigned area, then produce or update `<area-name>.overview.md`.

## Completion Criteria

This workflow is complete for the current invocation when:

- in `file-mapping` mode: the approved existing-file slice has been mapped under `input-project-documentation/`, each in-scope existing file has a current mapped input doc, and unchanged-but-load-bearing files remain explicitly visible
- in `overview-generation` mode: the requested `<area-name>.overview.md` artifact exists at the root of `input-project-documentation/` and accurately synthesizes the already-mapped area slice
- stale or out-of-scope mapped artifacts have been removed or explicitly retained pending scope approval
- the next consuming phase or artifact can use the input-project-documentation tree as the current-state foundation

## Notes

- On the initial research path, this workflow replaces the packet-first procedure of `delegated-research`, but it should preserve the same delegated dispatch and parallel deep-read posture.
- It changes the primary current-state artifact from packet-style exact-current-state outputs to a mapped documentation tree.
- Phase-specific workflows should invoke this as a reusable current-state mapping procedure rather than redefining it per phase.
