// L9 → L17: the compact coordination-notes ENTRY SURFACE inside the task reader. It lists the
// selected master's tasks/<repo>/<master>/notes/** tree (reports/ included) and resolves task-doc
// reference strings that name an existing notes file into openable links. Both surfaces OPEN THE
// NOTES READER (the L17 file-viewer-style takeover) via `onOpenNotes`; the bespoke inline reading
// pane that used to live here was RETIRED — reading a note now happens in the full reader view
// (panels/notes-reader/NotesReaderViewer.tsx), with the master's whole notes tree in its rail.
// Everything here is still GET-only component state: no store mutation, no write surface.
import { useEffect, useState } from "react";

import { css } from "../../styled-system/css";
import { listNotes, resolveNoteReference, type NotesListing } from "../data/notes";
import {
  requirementAddressFromReference,
  resolveRequirementReference,
  type RequirementEntry,
} from "../data/requirements";
import type { TaskArtifactReaderTarget as NotesReaderTarget } from "../data/taskArtifacts";
import { Markdown } from "../grammar/Markdown";
import { useTaskRequirementLinks } from "../grammar/TaskRequirementLinks";

const section = css({ display: "grid", gap: "0.3rem" });
const heading = css({
  margin: "0",
  fontSize: "0.72rem",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: "amber",
});
const bullets = css({
  margin: "0",
  paddingLeft: "1.1rem",
  maxWidth: "78ch",
  display: "grid",
  gap: "0.2rem",
  fontSize: "0.84rem",
  lineHeight: "1.45",
});
// A resolved reference: the whole reference string becomes the openable link into the notes reader.
const refLink = css({
  font: "inherit",
  textAlign: "left",
  color: "cyan",
  background: "transparent",
  border: "0",
  padding: "0",
  cursor: "pointer",
  textDecoration: "underline",
  _hover: { color: "ink" },
  _focusVisible: { outline: "1px solid token(colors.amber)", outlineOffset: "1px" },
});
const noteList = css({ listStyle: "none", margin: "0", padding: "0", display: "grid", gap: "0.15rem" });
const noteRow = css({
  display: "flex",
  justifyContent: "space-between",
  gap: "0.5rem",
  width: "100%",
  textAlign: "left",
  font: "inherit",
  fontSize: "0.78rem",
  paddingInline: "0.4rem",
  paddingBlock: "0.2rem",
  background: "bg",
  border: "0",
  borderLeftWidth: "2px",
  borderLeftStyle: "solid",
  borderLeftColor: "grid",
  color: "ink",
  cursor: "pointer",
  _hover: { background: "oklch(0.7 0.1 200 / 0.12)", borderLeftColor: "amber" },
  _focusVisible: { outline: "1px solid token(colors.amber)", outlineOffset: "1px" },
});
const noteMeta = css({ color: "muted", fontSize: "0.72rem" });
const listCapHint = css({ color: "muted", fontSize: "0.72rem" });

function ReferenceList({
  references,
  notePaths,
  requirements,
  openNote,
  openRequirement,
}: {
  references: string[];
  notePaths: string[];
  requirements: RequirementEntry[];
  openNote: (path: string) => void;
  openRequirement: (path: string) => void;
}) {
  return (
    <section className={section}>
      <h3 className={heading}>References</h3>
      <ul className={bullets}>
        {references.map((reference, index) => {
          const requirementAddress = requirementAddressFromReference(reference);
          const requirementTarget = resolveRequirementReference(reference, requirements);
          const noteTarget = requirementAddress
            ? undefined
            : resolveNoteReference(reference, notePaths);
          return (
            <li key={reference}>
              {requirementTarget ? (
                <button
                  type="button"
                  className={refLink}
                  onClick={() => openRequirement(requirementTarget)}
                  data-testid={`requirement-ref-${index + 1}`}
                  title={`open requirements/${requirementTarget}`}
                >
                  {reference}
                </button>
              ) : noteTarget ? (
                <button
                  type="button"
                  className={refLink}
                  onClick={() => openNote(noteTarget)}
                  data-testid={`note-ref-${index + 1}`}
                  title={`open notes/${noteTarget}`}
                >
                  <Markdown inline>{reference}</Markdown>
                </button>
              ) : (
                <Markdown inline>{reference}</Markdown>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function SeriesNotesList({
  notes,
  truncated,
  open,
}: {
  notes: NotesListing["notes"];
  truncated: boolean;
  open: (path: string) => void;
}) {
  return (
    <section className={section}>
      <h3 className={heading}>Series notes</h3>
      <ul className={noteList}>
        {notes.map((note, index) => (
          <li key={note.path}>
            <button
              type="button"
              className={noteRow}
              onClick={() => open(note.path)}
              data-testid={`note-open-${index + 1}`}
              title={`open notes/${note.path}`}
            >
              <span>{note.path}</span>
              <span className={noteMeta}>{note.size.toLocaleString()} B</span>
            </button>
          </li>
        ))}
      </ul>
      {truncated ? (
        <span className={listCapHint}>deeper subfolders exist but are beyond the list cap</span>
      ) : null}
    </section>
  );
}

// The series' notes surface + the doc's reference list, sharing one notes listing so a resolved
// reference and a list row both open the reader on the right note. An unreachable notes API (or a
// series without a notes/ folder — the server answers an empty list) simply renders no notes section;
// references then all stay plain text. Reading itself is delegated to `onOpenNotes` (the L17 reader).
export function TaskNotes({
  repo,
  master,
  references,
  onOpenNotes,
}: {
  repo: string;
  master: string;
  references: string[];
  // Open the L17 notes reader on a note. Optional so a context without the takeover (some tests /
  // the master-overview list) still renders the surface; the rows are then inert.
  onOpenNotes?: (target: NotesReaderTarget) => void;
}) {
  const [listing, setListing] = useState<NotesListing | null>(null);
  const requirementLinks = useTaskRequirementLinks();

  useEffect(() => {
    let live = true;
    setListing(null);
    void listNotes(repo, master).then(
      (data) => {
        if (live) setListing(data);
      },
      () => {}, // unreachable API: no notes surface, references stay plain text
    );
    return () => {
      live = false;
    };
  }, [repo, master]);

  const notes = listing?.notes ?? [];
  const notePaths = notes.map((note) => note.path);
  const openNote = (path: string) => onOpenNotes?.({ kind: "notes", repo, master, path });

  return (
    <>
      {references.length > 0 ? (
        <ReferenceList
          references={references}
          notePaths={notePaths}
          requirements={requirementLinks?.requirements ?? []}
          openNote={openNote}
          openRequirement={(path) => requirementLinks?.open(path)}
        />
      ) : null}
      {notes.length > 0 ? (
        <SeriesNotesList notes={notes} truncated={listing?.truncated === true} open={openNote} />
      ) : null}
    </>
  );
}
