// The Notes Reader screen: the coordination-notes reading experience rebuilt on the SAME
// full-view pattern the Change-Set / File Viewer use — a task-scoped takeover whose LEFT RAIL is the
// master's notes tree (from /api/notes/list, reports/ included) with the open note highlighted, and
// whose content pane renders the opened note by REUSING the File Viewer's `DualPane` primitive:
//   - a markdown note takes DualPane's partnerless-markdown path (code=null, sidecar=markdown) — the
//     exact path the File Viewer uses to render a partnerless route overview full-pane;
//   - a text note takes DualPane's CodeSide (read-only CodeMirror), same as any code file;
//   - a binary note degrades to DualPane's byte-count placeholder.
// There is no second bespoke reader: the layout is the ChangeSetViewer rail+pane+back idiom and the
// content is `DualPane`. The view is CONTROLLED — the open `path` and rail `onSelectNote` are lifted
// to CockpitShell (like the File Viewer's persisted state) so selection survives back/forward.
import { memo, useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

import { css } from "../../../styled-system/css";
import type { FileContent } from "../../data/files";
import { listNotes, readNote, type NoteContent, type NotesListing } from "../../data/notes";
import {
  listRequirements,
  readRequirement,
  type RequirementContent,
} from "../../data/requirements";
import type { TaskArtifactReaderTarget } from "../../data/taskArtifacts";
import { DualPane, type SidecarView } from "../file-viewer/DualPane";

export type { TaskArtifactReaderTarget } from "../../data/taskArtifacts";

// Screen chrome — the ChangeSetViewer takeover idiom (flex column · sticky back header · rail+pane).
const screen = css({ height: "100%", minHeight: "0", display: "flex", flexDirection: "column", background: "bg" });
const header = css({
  flexShrink: 0,
  display: "flex",
  alignItems: "center",
  gap: "0.8rem",
  paddingInline: "0.8rem",
  paddingBlock: "0.4rem",
  borderBottomWidth: "1px",
  borderBottomStyle: "solid",
  borderBottomColor: "grid",
  background: "bgPanel",
});
const back = css({
  fontFamily: "mono",
  fontSize: "0.74rem",
  color: "amber",
  background: "transparent",
  borderWidth: "1px",
  borderStyle: "solid",
  borderColor: "grid",
  borderRadius: "2px",
  paddingInline: "0.5rem",
  paddingBlock: "0.15rem",
  cursor: "pointer",
  _hover: { borderColor: "amber" },
});
const title = css({ fontSize: "0.82rem", color: "ink", fontWeight: "600" });
const openPath = css({ marginLeft: "auto", fontSize: "0.74rem", fontFamily: "mono", color: "cyan", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "50%" });
// Rail — the ChangeSetViewer changed-files column idiom (sticky section head + amber-wash active row).
const railCol = css({ height: "100%", minHeight: "0", display: "flex", flexDirection: "column", background: "bgPanel" });
const railScroll = css({ flex: "1", minHeight: "0", overflow: "auto" });
const railHead = css({
  position: "sticky",
  top: "0",
  background: "bg",
  paddingInline: "0.6rem",
  paddingBlock: "0.25rem",
  fontSize: "0.68rem",
  letterSpacing: "0.05em",
  textTransform: "uppercase",
  color: "amber",
  borderBottomWidth: "1px",
  borderBottomStyle: "solid",
  borderBottomColor: "grid",
});
const railRow = css({
  display: "flex",
  alignItems: "center",
  gap: "0.4rem",
  width: "100%",
  textAlign: "left",
  font: "inherit",
  fontSize: "0.74rem",
  fontFamily: "mono",
  color: "ink",
  background: "transparent",
  border: "0",
  paddingInline: "0.6rem",
  paddingBlock: "0.2rem",
  cursor: "pointer",
  _hover: { background: "color-mix(in oklab, var(--amber) 12%, transparent)", color: "amber" },
  "&[data-active=true]": { background: "color-mix(in oklab, var(--amber) 20%, transparent)", color: "cyan" },
});
const railPath = css({ flex: "1", minWidth: "0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
const railSize = css({ flexShrink: 0, color: "muted", fontSize: "0.68rem" });
const railHint = css({ paddingInline: "0.6rem", paddingBlock: "0.25rem", color: "muted", fontSize: "0.68rem" });
const handle = css({ width: "3px", flexShrink: "0", background: "grid", cursor: "col-resize", _hover: { background: "amber" } });
const placeholder = css({ height: "100%", display: "grid", placeItems: "center", padding: "1rem", color: "muted", fontSize: "0.8rem", textAlign: "center" });
// Content-pane fill + the truncation banner (mirrors DualPane's own `fill`/`banner`): a truncated
// MARKDOWN note takes DualPane's partnerless-markdown path, which has no banner (only CodeSide does),
// so a >2-MiB markdown note -- the dominant note type -- would silently drop its "showing the first
// 2 MiB" contract. We render the same banner here, above the pane, for that case.
const paneFill = css({ height: "100%", minHeight: "0", display: "flex", flexDirection: "column" });
const paneBody = css({ flex: "1", minHeight: "0" });
const truncBanner = css({
  flexShrink: 0,
  paddingInline: "0.6rem",
  paddingBlock: "0.2rem",
  fontSize: "0.72rem",
  color: "amber",
  background: "bgPanel",
  borderBottomWidth: "1px",
  borderBottomStyle: "solid",
  borderBottomColor: "grid",
});

// A fetched note mapped to DualPane's props: markdown renders full-pane as a partnerless markdown doc
// (the File Viewer's overview treatment); other text / binary render through CodeSide.
type ArtifactContent = NoteContent | (RequirementContent & { language: "markdown"; truncated: false });

function noteAsFileContent(note: ArtifactContent): FileContent {
  // CodeSide reads only language/size/truncated/content; scope/onboarding are inert for a note.
  return {
    scope: "",
    path: note.path,
    language: note.language,
    size: note.size,
    truncated: note.truncated,
    content: note.content,
    onboarding: { status: "missing" },
  };
}

function dualPaneProps(note: ArtifactContent): { code: FileContent | null; sidecar: SidecarView } {
  return note.language === "markdown"
    ? { code: null, sidecar: { state: "markdown", body: note.content } }
    : { code: noteAsFileContent(note), sidecar: { state: "empty" } };
}

function NotesRail({
  notes,
  truncated,
  activePath,
  onSelectNote,
  kind,
}: {
  notes: NotesListing["notes"];
  truncated: boolean;
  activePath: string;
  onSelectNote: (path: string) => void;
  kind: TaskArtifactReaderTarget["kind"];
}) {
  return (
    <div className={railCol}>
      <div className={railScroll} data-testid="notes-rail">
        <div className={railHead}>{kind} ({notes.length})</div>
        {notes.map((entry, index) => (
          <button
            key={entry.path}
            type="button"
            className={railRow}
            data-active={entry.path === activePath}
            data-testid={`note-rail-${index + 1}`}
            onClick={() => onSelectNote(entry.path)}
          >
            <span className={railPath}>{entry.path}</span>
            <span className={railSize}>{entry.size.toLocaleString()} B</span>
          </button>
        ))}
        {truncated ? (
          <div className={railHint}>deeper subfolders exist but are beyond the list cap</div>
        ) : null}
      </div>
    </div>
  );
}

function NotePane({ failed, note, kind }: {
  failed: boolean;
  note: ArtifactContent | null;
  kind: TaskArtifactReaderTarget["kind"];
}) {
  if (failed) {
    return (
      <div className={placeholder} data-testid="note-status">
        Could not load this {kind === "notes" ? "note" : "requirement"}.
      </div>
    );
  }
  if (note === null) {
    return (
      <div className={placeholder} data-testid="note-status">
        Loading…
      </div>
    );
  }
  return (
    <div className={paneFill}>
      {note.truncated && note.language === "markdown" ? (
        <div className={truncBanner} data-testid="notes-trunc-banner">
          Showing the first 2 MiB of {note.size.toLocaleString()} bytes
        </div>
      ) : null}
      <div className={paneBody}>
        <DualPane {...dualPaneProps(note)} split={false} />
      </div>
    </div>
  );
}

function useArtifactListing(
  repo: string,
  master: string,
  document: string | undefined,
): NotesListing | null {
  const [listing, setListing] = useState<NotesListing | null>(null);
  useEffect(() => {
    let live = true;
    setListing(null);
    const request = document === undefined
      ? listNotes(repo, master)
      : listRequirements(repo, master, document).then((data) => ({
          repo: data.repo,
          master: data.master,
          notes: data.requirements.map((entry) => ({ ...entry, language: "markdown" })),
          truncated: false,
        }));
    void request.then(
      (data) => live && setListing(data),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [document, master, repo]);
  return listing;
}

function useArtifactContent(
  repo: string,
  master: string,
  document: string | undefined,
  path: string,
): { failed: boolean; content: ArtifactContent | null } {
  const [content, setContent] = useState<ArtifactContent | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    setContent(null);
    setFailed(false);
    const request = document === undefined
      ? readNote(repo, master, path)
      : readRequirement(repo, master, document, path).then((data) => ({
          ...data,
          language: "markdown" as const,
          truncated: false as const,
        }));
    void request.then(
      (data) => live && setContent(data),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [document, master, path, repo]);
  return { failed, content };
}

function NotesReaderViewerImpl(
  props: TaskArtifactReaderTarget & { onSelectNote: (path: string) => void; onBack: () => void },
) {
  const { kind, repo, master, path, onSelectNote, onBack } = props;
  const document = kind === "requirements" ? props.document : undefined;
  const listing = useArtifactListing(repo, master, document);
  const { failed, content } = useArtifactContent(repo, master, document, path);

  const notes = listing?.notes ?? [];

  return (
    <div className={screen} data-testid="notes-reader-viewer" data-artifact-kind={kind}>
      <header className={header}>
        <button type="button" className={back} onClick={onBack} data-testid="notes-reader-back">
          ← back
        </button>
        <span className={title}>{kind} · {master}</span>
        <span className={openPath} data-testid="notes-reader-open">{kind}/{path}</span>
      </header>
      <PanelGroup direction="horizontal" autoSaveId="notesreader.outer" className={css({ flex: "1", minHeight: "0" })}>
        <Panel defaultSize={26} minSize={16}>
          <NotesRail
            notes={notes}
            truncated={listing?.truncated === true}
            activePath={path}
            onSelectNote={onSelectNote}
            kind={kind}
          />
        </Panel>
        <PanelResizeHandle className={handle} />
        <Panel minSize={30}>
          <NotePane failed={failed} note={content} kind={kind} />
        </Panel>
      </PanelGroup>
    </div>
  );
}

// Memoized (tab-switch CPU): kept mounted (hidden) once opened — the shell re-renders on
// every view switch with unchanged props, and the memo gate skips this subtree then; the reader's
// own state still drives its updates.
export const NotesReaderViewer = memo(NotesReaderViewerImpl);
