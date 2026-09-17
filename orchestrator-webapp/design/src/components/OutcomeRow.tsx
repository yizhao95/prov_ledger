import * as React from "react";
import tokens from "../tokens";
import {Meta, Hidden, c} from "../primitives";
import {TierBadge, Tier} from "./TierBadge";

export interface OutcomeRowProps {
  /** expectations.claim — what the plan said would be true. */
  claim: string;
  /** expectations.target + target_kind. */
  target: string;
  targetKind?: string;
  /** The latest outcome recorded against it, or null when none was. */
  latest?: {
    kind: "observed" | "survival" | "none_available" | "pending" | string;
    tier?: Tier | string;
    signal?: string | null;
    delta_pct?: number | null;
    summary?: string;
    at?: string;
    planId?: string | null;
  } | null;
}

const KIND_WORDS: Record<string, string> = {
  observed: "observed", survival: "survival", none_available: "nothing to observe", pending: "pending",
};

/**
 * One claim across plans and how it actually turned out. `none_available` and
 * `pending` are printed, not blanked — "we never checked" is a different fact
 * from "it was fine", and the ledger refuses to blur them.
 */
export function OutcomeRow({claim, target, targetKind, latest}: OutcomeRowProps) {
  const kind = latest?.kind ?? "pending";
  const bad = latest?.signal && !["untouched", "untouched_consumed", "survived"].includes(latest.signal);
  return (
    <div data-outcome={kind} data-target={target}
         style={{display: "grid", gridTemplateColumns: "1fr auto", gap: tokens.spacing.row,
                 padding: `${tokens.spacing.row} 0`, borderBottom: `1px solid ${c.hairline}`}}>
      <div>
        <div style={{color: c["brand-gray"]}}>“{claim}”</div>
        <Hidden id={`${targetKind ?? "node"}:${target}`}>
          <Meta>on {target.split(".").pop()}</Meta>
        </Hidden>
      </div>
      <div style={{textAlign: "right", whiteSpace: "nowrap"}}>
        <span style={{color: bad ? c["brand-red"] : c["brand-gray"], fontSize: tokens.type_scale.meta}}>
          {KIND_WORDS[kind] ?? kind}
          {latest?.signal ? ` · ${latest.signal}` : ""}
          {typeof latest?.delta_pct === "number" ? ` · ${latest.delta_pct > 0 ? "+" : ""}${latest.delta_pct}%` : ""}
        </span>
        {latest?.tier && <> <TierBadge tier={latest.tier} /></>}
        {latest?.at && <div><Meta>{latest.at.slice(0, 10)}</Meta></div>}
      </div>
    </div>
  );
}

export default OutcomeRow;
