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
  blocking: "必须回应", warning: "值得注意", info: "仅供参考",
};

/**
 * 开工前的提醒 — what the two-layer check found before this plan touched
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
        <span style={{fontSize: tokens.type_scale.body}}>条必须回应还没回应</span>
        <Meta>· 共 {findings.length} 条提醒{typeof summary.shown === "number" ? ` · 被看到 ${summary.shown} 次` : ""}
          {typeof summary.adopted === "number" ? ` · 改变了 ${summary.adopted} 次计划` : ""}</Meta>
      </div>
      {findings.length === 0 && (
        <Meta>两层都查过这些目标，账上没有任何相关记录。</Meta>
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
              <Meta>→ {f.response.action === "revise" ? "改了计划" : "照做了"}（{f.response.by}）
                {f.response.rationale ? ` · ${f.response.rationale}` : ""}
                {f.agent_proceeded ? " · agent 越过" : ""}</Meta>
            ) : f.unanswered ? (
              <span data-unanswered-finding="1"
                    style={{fontSize: tokens.type_scale.micro, color: c["brand-red"]}}>未回答</span>
            ) : null}
          </div>
        ))}
      </div>
      {findings.length > 3 && (
        <div style={{marginTop: tokens.spacing.row}}>
          <Disclose summary="这些提醒是怎么算出来的" >
            <Meta>两层：这个目标自己的历史（规矩、走不通的路、上次失败的声明、上游被删），
              和它的波及范围（谁吃它的输出、哪些上游没核对过、下游有哪些规矩）。</Meta>
          </Disclose>
        </div>
      )}
    </Card>
  );
}

export default HeadlineBlock;
