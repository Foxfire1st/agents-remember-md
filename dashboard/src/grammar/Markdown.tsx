import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { css } from "../../styled-system/css";
import { isRequirementAddress, resolveRequirementAddress } from "../data/requirements";
import { useTaskRequirementLinks } from "./TaskRequirementLinks";

// Markdown rendering for task/master prose (slice 6g). The task-doc section bodies are GFM markdown
// (tables, blockquotes, **bold**, `code`, lists, links) folded from the original task.md; without a
// renderer they showed raw. Styled via Panda descendant selectors so we don't hand-wrap every node;
// a wide table gets a horizontal scroll box so it can't blow out the detail panel. react-markdown
// renders no raw HTML by default, so this is safe for arbitrary doc content.
const box = css({
  fontSize: "0.86rem",
  lineHeight: "1.55",
  color: "ink",
  "& > :first-child": { marginTop: "0" },
  "& > :last-child": { marginBottom: "0" },
  "& p": { margin: "0 0 0.5rem", maxWidth: "78ch" },
  "& strong": { fontWeight: "600", color: "ink" },
  "& em": { fontStyle: "italic" },
  "& a": { color: "cyan", textDecoration: "underline" },
  "& del": { textDecoration: "line-through", color: "muted" },
  "& code": {
    fontSize: "0.86em",
    background: "bg",
    paddingInline: "0.25rem",
    paddingBlock: "0.05rem",
    borderRadius: "2px",
  },
  "& pre": {
    background: "bg",
    padding: "0.5rem 0.6rem",
    borderRadius: "3px",
    overflow: "auto",
    margin: "0.3rem 0",
  },
  "& pre code": { background: "transparent", paddingInline: "0", fontSize: "0.78rem" },
  "& blockquote": {
    margin: "0.4rem 0",
    paddingLeft: "0.6rem",
    borderLeftWidth: "2px",
    borderLeftStyle: "solid",
    borderLeftColor: "grid",
    color: "muted",
  },
  "& ul, & ol": { margin: "0 0 0.5rem", paddingLeft: "1.2rem", display: "grid", gap: "0.15rem" },
  "& li": { maxWidth: "78ch" },
  "& h1, & h2, & h3, & h4": {
    fontSize: "0.78rem",
    letterSpacing: "0.04em",
    textTransform: "uppercase",
    color: "amber",
    margin: "0.6rem 0 0.3rem",
  },
  "& table": { borderCollapse: "collapse", fontSize: "0.8rem" },
  "& th, & td": {
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: "grid",
    padding: "0.2rem 0.45rem",
    textAlign: "left",
    verticalAlign: "top",
  },
  "& th": { color: "amber", fontWeight: "600", whiteSpace: "nowrap" },
});
const tableScroll = css({ overflowX: "auto", maxWidth: "100%", margin: "0.3rem 0" });
const internalLink = css({
  font: "inherit",
  color: "cyan",
  background: "transparent",
  border: "0",
  padding: "0",
  cursor: "pointer",
  textDecoration: "underline",
});

// Inline variant for list items / decision cells: the markdown is a single phrase, so unwrap the
// paragraph (no block margins, no <p>-in-<span>) and keep bold/code/links/em.
const inlineBox = css({
  "& code": { background: "bg", paddingInline: "0.2rem", borderRadius: "2px", fontSize: "0.86em" },
  "& strong": { fontWeight: "600", color: "ink" },
  "& em": { fontStyle: "italic" },
  "& del": { textDecoration: "line-through", color: "muted" },
  "& a": { color: "cyan", textDecoration: "underline" },
});

const blockComponents: Components = {
  // Keep the table a real <table> but inside a scroll box so wide tables never overflow the panel.
  table({ node, ...props }) {
    void node; // strip the AST node react-markdown passes; only DOM props go to <table>
    return (
      <div className={tableScroll}>
        <table {...props} />
      </div>
    );
  },
};

const inlineComponents: Components = {
  // Unwrap the single paragraph markdown produces for a one-line phrase.
  p: ({ children }) => <>{children}</>,
};

// memo: the projection ticks (SSE) re-render DetailPanel every ~second, but section bodies are
// stable strings — without memo, react-markdown re-parses every body on every tick (scroll jank).
// children is a primitive string, so the default shallow prop compare is exactly right here.
export const Markdown = memo(function Markdown({
  children,
  inline = false,
}: {
  children: string;
  inline?: boolean;
}) {
  const requirementLinks = useTaskRequirementLinks();
  const requirementAnchor: Components["a"] = ({ node, href = "", children: linkChildren, ...props }) => {
    void node;
    const target = requirementLinks
      ? resolveRequirementAddress(href, requirementLinks.requirements)
      : undefined;
    if (target) {
      return (
        <button
          type="button"
          className={internalLink}
          data-testid="requirement-link"
          title={`open requirements/${target}`}
          onClick={() => requirementLinks?.open(target)}
        >
          {linkChildren}
        </button>
      );
    }
    if (isRequirementAddress(href)) {
      return <span data-testid="requirement-link-refused">{linkChildren}</span>;
    }
    return <a href={href} {...props}>{linkChildren}</a>;
  };
  const components = inline
    ? { ...inlineComponents, a: requirementAnchor }
    : { ...blockComponents, a: requirementAnchor };
  if (inline) {
    return (
      <span className={inlineBox}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {children}
        </ReactMarkdown>
      </span>
    );
  }
  return (
    <div className={box}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
});
