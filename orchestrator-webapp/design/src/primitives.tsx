/* Shared primitives — the layout spec of DP phase 2d in three pieces.
 *
 *  Column   one column, read top to bottom, max 880px (spec 1)
 *  Rail     a vertical line with one dot per row; a start and an end (spec 2)
 *  Disclose expands IN PLACE — a decision point never navigates away (spec 5)
 */
import * as React from "react";
import tokens from "./tokens";

export const c = tokens.colors;

export function Column({children, style}: {children?: React.ReactNode; style?: React.CSSProperties}) {
  return (
    <div style={{maxWidth: tokens.layout.column_max, margin: "0 auto", padding: tokens.spacing.gutter,
                 background: c["brand-bg"], color: c["brand-gray"],
                 fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif",
                 fontSize: tokens.type_scale.body, lineHeight: 1.55, ...style}}>
      {children}
    </div>
  );
}

/** One row on the rail: the line runs through it, the dot marks it, content sits right. */
export function RailRow({dot = c["brand-muted"], head, children, last = false, emphasis = false}: {
  dot?: string; head?: React.ReactNode; children?: React.ReactNode; last?: boolean; emphasis?: boolean;
}) {
  return (
    <div style={{display: "grid", gridTemplateColumns: "14px 1fr", columnGap: tokens.spacing.row}}>
      <div style={{position: "relative", display: "flex", justifyContent: "center"}}>
        <span style={{position: "absolute", top: 0, bottom: last ? "50%" : 0, width: 2, background: c.line}} />
        <span style={{position: "relative", marginTop: 6, width: emphasis ? 10 : 8, height: emphasis ? 10 : 8,
                      borderRadius: tokens.radii.dot, background: dot,
                      boxShadow: emphasis ? `0 0 0 3px ${c["blue-wash"]}` : undefined}} />
      </div>
      <div style={{paddingBottom: tokens.spacing.card}}>
        {head}
        {children}
      </div>
    </div>
  );
}

/** The rail's terminals — a node is born and a node is where it is now (spec 2). */
export function RailEnd({label, kind = "start"}: {label: string; kind?: "start" | "end"}) {
  return (
    <div style={{display: "grid", gridTemplateColumns: "14px 1fr", columnGap: tokens.spacing.row}}>
      <div style={{position: "relative", display: "flex", justifyContent: "center"}}>
        <span style={{position: "absolute", top: kind === "start" ? "50%" : 0, bottom: kind === "start" ? 0 : "50%",
                      width: 2, background: c.line}} />
        <span style={{position: "relative", marginTop: 4, width: 12, height: 12, borderRadius: tokens.radii.dot,
                      border: `2px solid ${c.line}`, background: c.surface}} />
      </div>
      <div style={{fontSize: tokens.type_scale.micro, color: c["brand-muted"], textTransform: "uppercase",
                   letterSpacing: "0.06em", paddingBottom: tokens.spacing.row, paddingTop: 2}}>{label}</div>
    </div>
  );
}

export function Card({children, accent, style, ...rest}: {
  children?: React.ReactNode; accent?: string; style?: React.CSSProperties;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div {...rest} style={{background: c.surface, border: `1px solid ${c.hairline}`, borderRadius: tokens.radii.card,
                 borderLeft: accent ? `3px solid ${accent}` : undefined,
                 padding: tokens.spacing.card, ...style}}>{children}</div>
  );
}

export function Meta({children, title}: {children?: React.ReactNode; title?: string}) {
  return <span title={title} style={{fontSize: tokens.type_scale.meta, color: c["brand-muted"]}}>{children}</span>;
}

/** An id the ledger needs and a reader does not: hidden in a tooltip (spec 8). */
export function Hidden({id, children}: {id: string; children?: React.ReactNode}) {
  return <span title={id} data-id={id} style={{borderBottom: `1px dotted ${c.hairline}`}}>{children}</span>;
}

/** Expand in place. Never a new page (spec 5). */
export function Disclose({summary, count, children, open: initial = false}: {
  summary: React.ReactNode; count?: number; children?: React.ReactNode; open?: boolean;
}) {
  const [open, setOpen] = React.useState(initial);
  return (
    <div>
      <button onClick={() => setOpen(!open)} data-disclose={open ? "open" : "closed"}
              style={{all: "unset", cursor: "pointer", color: c["brand-blue"], fontSize: tokens.type_scale.meta}}>
        {open ? "▾" : "▸"} {summary}{typeof count === "number" ? ` (${count})` : ""}
      </button>
      {open && <div style={{marginTop: tokens.spacing.tight, paddingLeft: tokens.spacing.row,
                            borderLeft: `2px solid ${c.hairline}`}}>{children}</div>}
    </div>
  );
}

/** A significant change, shown as before → after rather than the word "changed" (spec 3). */
export function BeforeAfter({before, after, label}: {before: string; after: string; label?: string}) {
  return (
    <div data-beforeafter style={{display: "grid", gridTemplateColumns: "1fr 16px 1fr", alignItems: "center",
                                  gap: tokens.spacing.tight, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                                  fontSize: tokens.type_scale.micro, marginTop: tokens.spacing.tight}}>
      <div style={{background: c["brand-bg"], border: `1px solid ${c.hairline}`, borderRadius: tokens.radii.chip,
                   padding: "4px 6px", textDecoration: "line-through", color: c["brand-muted"]}}>{before}</div>
      <div style={{textAlign: "center", color: c["brand-muted"]}}>→</div>
      <div style={{background: c["spark-wash"], border: `1px solid ${c["brand-spark"]}`, borderRadius: tokens.radii.chip,
                   padding: "4px 6px", color: c["spark-ink"]}}>{after}</div>
      {label && <div style={{gridColumn: "1 / -1", color: c["brand-muted"]}}>{label}</div>}
    </div>
  );
}
