import * as React from "react";
import tokens from "../tokens";
import {Meta, Disclose, c} from "../primitives";

export interface Record_ {
  reason_id: number;
  text: string;
  node_key?: string | null;
  role?: string | null;
  /** read_hit.moment — plan | edit | close | why */
  moment?: string;
  /** influence.via */
  via?: string;
  by?: string;
  at?: string;
}

export interface ShownAdoptedProps {
  /** read_hit rows — 被看到. */
  shown: Record_[];
  /** influence rows — 改变了计划. */
  adopted: Record_[];
}

const MOMENTS: Record<string, string> = {
  plan: "写计划时", edit: "动手改之前", close: "收尾时", why: "有人来查时",
};

/**
 * 被看到 vs 改变了计划 — two counts that are NEVER derived from each other
 * (I11). A record shown in a headline is a read_hit; only an id an agent
 * actually cited becomes an influence. Nothing here claims anyone READ
 * anything: the system can record that something was shown, not that it
 * was understood.
 */
export function ShownAdopted({shown, adopted}: ShownAdoptedProps) {
  return (
    <div data-panel="shown-adopted" style={{display: "grid", gap: tokens.spacing.row}}>
      <Disclose summary={`被看到 ${shown.length} 次`} count={undefined}>
        {shown.length === 0 && <Meta>没有展示过。</Meta>}
        {shown.map((r, i) => (
          <div key={`${r.reason_id}-${i}`} data-shown={r.reason_id} style={{marginBottom: 4}}>
            <Meta>{MOMENTS[r.moment ?? ""] ?? r.moment} · </Meta>
            <span style={{color: c["brand-gray"], fontSize: tokens.type_scale.meta}}>“{r.text}”</span>
          </div>
        ))}
      </Disclose>
      <Disclose summary={`改变了 ${adopted.length} 次计划`} open={adopted.length > 0}>
        {adopted.length === 0 && <Meta>本次没有采用任何历史记录。</Meta>}
        {adopted.map((r, i) => (
          <div key={`${r.reason_id}-${i}`} data-adopted={r.reason_id} style={{marginBottom: 4}}>
            <span style={{color: c["brand-gray"], fontSize: tokens.type_scale.meta}}>“{r.text}”</span>{" "}
            <Meta title={`via ${r.via ?? "?"}`}>（{r.by ?? "agent"} 引用）</Meta>
          </div>
        ))}
      </Disclose>
    </div>
  );
}

export default ShownAdopted;
