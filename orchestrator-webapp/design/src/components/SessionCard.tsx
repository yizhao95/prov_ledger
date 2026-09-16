import * as React from "react";
import tokens from "../tokens";
import {Card, Meta, Disclose, Hidden, c} from "../primitives";

export interface SessionCardProps {
  sessionId: string;
  /** utterance rows; a personal one is marked and shown only on this machine. */
  utterances: {id: number; text: string; at?: string; visibility?: string}[];
  /** tool_call_log count for the session. */
  calls: number;
  /** The two buckets: what part of the work was provLedger itself. */
  buckets: {orchestration?: number; provenance?: number; other?: number};
  /** node_keys the session's graph refresh changed. */
  changedNodes?: {node_key: string; qualified_name?: string}[];
  plans: {plan_id: string; title?: string; status?: string}[];
  /** No plan was published — said out loud, not hidden. */
  degraded?: boolean;
}

/**
 * What a session said, what it cost, what it changed, what it published. The
 * degraded case — a session that never published a plan — is the one this card
 * exists for: it is labelled 降级（无 plan）rather than left off the list.
 */
export function SessionCard(p: SessionCardProps) {
  const pct = (n?: number) => (p.calls ? Math.round(((n ?? 0) / p.calls) * 100) : 0);
  return (
    <Card accent={p.degraded ? c["brand-serious"] : undefined}
          data-panel="session-head" data-session={p.sessionId}
          {...(p.degraded ? {"data-degraded": "1"} : {})}>
      <div style={{display: "flex", alignItems: "baseline", flexWrap: "wrap", gap: 8}}>
        <Hidden id={p.sessionId}>
          <span style={{fontSize: tokens.type_scale.title, fontWeight: 600}}>
            会话 {p.sessionId.slice(0, 8)}
          </span>
        </Hidden>
        {p.degraded && (
          <span data-degraded-badge
                style={{fontSize: tokens.type_scale.micro, padding: "1px 6px", borderRadius: tokens.radii.chip,
                        background: c["brand-serious"] + "26", color: c["serious-ink"]}}>
            降级（没发布计划）
          </span>
        )}
      </div>
      <div style={{marginTop: tokens.spacing.row, display: "grid", gap: tokens.spacing.row}}>
        <div>
          <Meta>说了什么</Meta>
          {p.utterances.length === 0 && <div><Meta>没有记到原话。</Meta></div>}
          {p.utterances.slice(0, 3).map((u) => (
            <div key={u.id} data-utterance={u.id} style={{color: c["brand-gray"]}}>
              “{u.text.length > 160 ? u.text.slice(0, 160) + "…" : u.text}”
              {u.visibility === "personal" && <Meta> · 只在本机显示</Meta>}
            </div>
          ))}
          {p.utterances.length > 3 && (
            <Disclose summary="还有" count={p.utterances.length - 3}>
              {p.utterances.slice(3).map((u) => (
                <div key={u.id} data-utterance={u.id}><Meta>“{u.text.slice(0, 160)}”</Meta></div>
              ))}
            </Disclose>
          )}
        </div>
        <div>
          <Meta>花了什么</Meta>
          <div data-calls={p.calls}>
            {p.calls} 次工具调用
            <Meta> · 编排 {pct(p.buckets.orchestration)}% · 溯源 {pct(p.buckets.provenance)}%</Meta>
          </div>
        </div>
        <div>
          <Meta>改了什么</Meta>
          <div data-changed={p.changedNodes?.length ?? 0}>
            {p.changedNodes && p.changedNodes.length > 0
              ? <Disclose summary={`${p.changedNodes.length} 个东西变了`}>
                  {p.changedNodes.map((n) => (
                    <div key={n.node_key}><Hidden id={n.node_key}>
                      <Meta>{n.qualified_name ?? n.node_key}</Meta></Hidden></div>
                  ))}
                </Disclose>
              : <Meta>这次会话没有刷新过图。</Meta>}
          </div>
        </div>
        <div>
          <Meta>发布了什么</Meta>
          {p.plans.length === 0 && <div><Meta>没有计划——所以这张卡片存在。</Meta></div>}
          {p.plans.map((pl) => (
            <div key={pl.plan_id}>
              <a href="#" data-plan={pl.plan_id} title={pl.plan_id}
                 style={{color: c["brand-blue"], textDecoration: "none"}}>
                《{pl.title ?? pl.plan_id}》
              </a>
              <Meta> · {pl.status}</Meta>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

export default SessionCard;
