import * as React from "react";
import tokens from "../tokens";
import {Card, Meta, Disclose, c} from "../primitives";
import {TierBadge, Tier} from "./TierBadge";

export interface Finding {
  id: string;
  kind: string;
  severity: "blocking" | "warning" | "info";
  tier: Tier | string;
  text: string;
  anchor?: string;
  unanswered?: boolean;
  agent_proceeded?: boolean;
  response?: {action: "revise" | "proceed"; by: string; rationale?: string | null} | null;
}

export interface HeadlineSummary {
  targets?: number; layers?: number; findings?: number;
  unanswered: number; answered?: number; proceeded_by_agent?: number;
  shown?: number; adopted?: number;
}

export interface HeadlineBlockProps {
  /** queries.get_headline()["summary"] */
  summary: HeadlineSummary;
  /** queries.get_headline()["findings"] */
  findings: Finding[];
}

const SEVERITY_WORDS: Record<string, string> = {
  blocking: "Blocking", warning: "Warning", info: "Info",
};

/**
 * Findings — what the two-layer check found before this plan touched
 * anything. It never blocks: an unanswered blocking finding is COUNTED and
 * shown in red, and an agent that proceeded past one is marked as such. The
 * page's one large number is the unanswered count (layout spec 4).
 */
export function HeadlineBlock({summary, findings}: HeadlineBlockProps) {
  const open = summary.unanswered > 0;
  return (
    <Card accent={open ? c["brand-red"] : c["brand-blue"]} data-panel="headline">
      <div style={{display: "flex", alignItems: "baseline", gap: tokens.spacing.row}}>
        <span style={{fontSize: tokens.type_scale.hero, fontWeight: 600,
                      color: open ? c["brand-red"] : c["brand-gray"]}} data-unanswered={summary.unanswered}>
          {summary.unanswered}
        </span>
        <span style={{fontSize: tokens.type_scale.body}}>blocking findings unanswered</span>
        <Meta>· {findings.length} findings{typeof summary.shown === "number" ? ` · Surfaced ${summary.shown}` : ""}
          {typeof summary.adopted === "number" ? ` · Adopted ${summary.adopted}` : ""}</Meta>
      </div>
      {findings.length === 0 && (
        <Meta>Both layers checked these targets; the ledger holds no related record.</Meta>
      )}
      <div style={{marginTop: tokens.spacing.row, display: "grid", gap: tokens.spacing.row}}>
        {findings.map((f) => (
          <div key={f.id} data-finding={f.id} data-severity={f.severity}
               style={{borderLeft: `2px solid ${f.unanswered ? c["brand-red"] : c.hairline}`,
                       paddingLeft: tokens.spacing.row}}>
            <div style={{display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: 6}}>
              <span style={{fontWeight: 600, color: f.severity === "blocking" ? c["brand-red"] : c["brand-gray"],
                            fontSize: tokens.type_scale.meta}}>
                {tokens.severities[f.severity]?.icon} {SEVERITY_WORDS[f.severity] ?? f.severity}
              </span>
              <TierBadge tier={f.tier} />
              <span style={{color: c["brand-gray"]}}>{f.text}</span>
            </div>
            {f.response ? (
              <Meta>→ {f.response.action === "revise" ? "plan revised" : "proceeded"} ({f.response.by})
                {f.response.rationale ? ` · ${f.response.rationale}` : ""}
                {f.agent_proceeded ? " · agent proceeded" : ""}</Meta>
            ) : f.unanswered ? (
              <span data-unanswered-finding="1"
                    style={{fontSize: tokens.type_scale.micro, color: c["brand-red"]}}>unanswered</span>
            ) : null}
          </div>
        ))}
      </div>
      {findings.length > 3 && (
        <div style={{marginTop: tokens.spacing.row}}>
          <Disclose summary="How these findings are computed" >
            <Meta>Two layers: the target's own history (constraints, rejected alternatives, claims that
              failed last time, upstreams that were removed), and its blast radius (what consumes its
              output, which upstreams were never checked, which constraints apply downstream).</Meta>
          </Disclose>
        </div>
      )}
    </Card>
  );
}

export default HeadlineBlock;
