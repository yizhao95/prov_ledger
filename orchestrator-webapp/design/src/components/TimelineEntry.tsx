import * as React from "react";
import tokens from "../tokens";
import {RailRow, Meta, Disclose, BeforeAfter, Hidden, c} from "../primitives";
import {TierBadge, Tier} from "./TierBadge";
import {SourceLevelBadge, SourceLevel} from "./SourceLevelBadge";

export interface TimelineEntryProps {
  /** The analysis run this moment belongs to (shown as a date, id in the tooltip). */
  run: {id: number; at?: string; planId?: string | null; planTitle?: string | null};
  /** node_event.event_type, e.g. node_changed / column_dropped. */
  event: string;
  tier: Tier | string;
  /** The words recorded at the time — verbatim when the user said them. */
  because?: {text: string; verbatim?: boolean; level?: SourceLevel | string; by?: string} | null;
  /** read_hit per moment — never summed across moments. */
  shown?: {plan?: number; edit?: number; why?: number};
  /** influence — the plans this record changed. */
  adoptedBy?: {planId: string; title?: string}[];
  /** A significant change shows what it actually was (layout spec 3). */
  diff?: {before: string; after: string; label?: string} | null;
  last?: boolean;
  emphasis?: boolean;
}

const EVENT_WORDS: Record<string, string> = {
  node_added: "新增", node_changed: "改动", node_renamed: "改名", node_moved: "移动",
  column_dropped: "列被移除", node_removed: "被删除", removed: "被删除",
  identity_asserted: "身份由模型判定", node_matched: "无变化", identity_kept: "无变化",
};

/**
 * One moment in a node's life: one row, one dot, the line running through it
 * (layout spec 2). The event is named in plain words; a significant change can
 * expand to before → after in place; the words recorded at the time sit under
 * it in quotes, and "被看到 / 改变了谁的计划" are separate counts that are never
 * derived from each other.
 */
export function TimelineEntry(p: TimelineEntryProps) {
  const word = EVENT_WORDS[p.event] ?? p.event;
  const dot = (tokens.tiers as Record<string, {dot: string}>)[p.tier as string]?.dot ?? c["brand-muted"];
  const shownTotal = Object.values(p.shown ?? {}).reduce((a, b) => a + (b ?? 0), 0);
  return (
    <RailRow dot={dot} last={p.last} emphasis={p.emphasis}
      head={
        <div style={{display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: 6}}>
          <Meta>{p.run.at?.slice(0, 10) ?? "—"}</Meta>
          <span style={{fontWeight: p.emphasis ? 600 : 500,
                        fontSize: p.emphasis ? tokens.type_scale.title : tokens.type_scale.body}}
                data-event={p.event}>{word}</span>
          <TierBadge tier={p.tier} />
          <Hidden id={`run ${p.run.id}`}><Meta>第 {p.run.id} 次分析</Meta></Hidden>
        </div>
      }>
      {p.diff && <BeforeAfter before={p.diff.before} after={p.diff.after} label={p.diff.label} />}
      {p.because ? (
        <div style={{marginTop: tokens.spacing.tight}}>
          <span style={{color: c["brand-gray"]}}>因为：“{p.because.text}”</span>{" "}
          {p.because.verbatim
            ? <span data-verbatim="1" style={{fontSize: tokens.type_scale.micro, color: c["brand-blue"]}}>你的原话</span>
            : <Meta>{p.because.by ?? "agent"} 的判断</Meta>}{" "}
          {p.because.level && <SourceLevelBadge level={p.because.level} />}
        </div>
      ) : (
        <Meta>当时未说明</Meta>
      )}
      <div style={{marginTop: tokens.spacing.tight, display: "flex", flexWrap: "wrap", gap: 10}}>
        {p.run.planId && (
          <a href="#" data-task-link={p.run.planId} title={p.run.planId}
             style={{color: c["brand-blue"], fontSize: tokens.type_scale.meta, textDecoration: "none"}}>
            回到当时的任务《{p.run.planTitle ?? p.run.planId}》 →
          </a>
        )}
        <Meta>被看到 {shownTotal} 次</Meta>
        {p.adoptedBy && p.adoptedBy.length > 0 && (
          <Disclose summary={`改变了 ${p.adoptedBy.length} 次计划`}>
            {p.adoptedBy.map((a) => (
              <div key={a.planId}>
                <Meta>这条记录改变了《{a.title ?? a.planId}》的计划</Meta>
              </div>
            ))}
          </Disclose>
        )}
      </div>
    </RailRow>
  );
}

export default TimelineEntry;
