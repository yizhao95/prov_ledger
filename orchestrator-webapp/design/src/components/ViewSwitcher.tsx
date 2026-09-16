import * as React from "react";
import tokens from "../tokens";
import {Meta, c} from "../primitives";

export interface Triple {
  /** The registered project. */
  project?: string | null;
  /** A qualified name or node_key. */
  node?: string | null;
  /** DP phase 2d: typed — "run:<id>" | "reason:<id>" | a plan id. */
  at?: string | null;
}

export interface ViewSwitcherProps {
  current: "graph" | "node" | "task" | "session" | string;
  triple: Triple;
  /** queries.view_bar()["links"] — null when a view has no anchor to go to. */
  links?: Partial<Record<"graph" | "node" | "task", string | null>>;
  /** Task 3c: the read-only search box that shares `why`'s FTS5 query. */
  onSearch?: (q: string) => void;
}

const VIEWS: [string, string, string][] = [
  ["graph", "🕸", "整张图"],
  ["node", "🧬", "这个东西"],
  ["task", "📋", "那次任务"],
];

/**
 * The one bar that carries the context triple across the three views. A view
 * with no anchor is DISABLED with a reason, not hidden — "there is no plan in
 * this context" is information. The breadcrumb shows names; the ids live in the
 * tooltip (layout spec 8).
 */
export function ViewSwitcher({current, triple, links = {}, onSearch}: ViewSwitcherProps) {
  const [q, setQ] = React.useState("");
  return (
    <nav data-view-bar data-view={current} data-project={triple.project ?? ""}
         data-node={triple.node ?? ""} data-at={triple.at ?? ""}
         style={{display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between",
                 gap: tokens.spacing.row, background: c.surface, borderBottom: `1px solid ${c.hairline}`,
                 padding: `${tokens.spacing.tight} ${tokens.spacing.gutter}`}}>
      <div style={{display: "flex", gap: 4}}>
        {VIEWS.map(([v, icon, label]) => {
          const href = links[v as "graph"];
          const on = v === current;
          const style: React.CSSProperties = {
            padding: "3px 10px", borderRadius: tokens.radii.chip, fontSize: tokens.type_scale.meta,
            border: `1px solid ${on ? c["brand-blue"] : c.hairline}`,
            background: on ? c["brand-blue"] : c.surface,
            color: on ? c.surface : href ? c["brand-blue"] : c["brand-muted"],
            textDecoration: "none",
          };
          return href ? (
            <a key={v} href={href} data-view-link={v} style={style}>{icon} {label}</a>
          ) : (
            <span key={v} data-view-link={v} data-view-disabled="1" style={style}
                  title={`当前上下文里没有${v === "node" ? "节点" : v === "graph" ? "项目" : "任务"}`}>{icon} {label}</span>
          );
        })}
      </div>
      <div style={{display: "flex", alignItems: "center", gap: tokens.spacing.row}}>
        {onSearch && (
          <input value={q} placeholder="搜原话、理由、规矩…" data-search
                 onChange={(e) => setQ(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && onSearch(q)}
                 style={{border: `1px solid ${c.hairline}`, borderRadius: tokens.radii.chip,
                         padding: "3px 8px", fontSize: tokens.type_scale.meta, background: c["brand-bg"],
                         color: c["brand-gray"]}} />
        )}
        <Meta>
          <span data-breadcrumb title={`${triple.project ?? "—"} / ${triple.node ?? "—"} / ${triple.at ?? "latest"}`}>
            {triple.project ?? "—"} › {(triple.node ?? "—").split(".").pop()} › {triple.at ?? "最新"}
          </span>
        </Meta>
      </div>
    </nav>
  );
}

export default ViewSwitcher;
