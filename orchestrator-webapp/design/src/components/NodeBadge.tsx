import * as React from "react";
import tokens from "../tokens";
import {c} from "../primitives";
import {Tier} from "./TierBadge";

export interface NodeBadgeProps {
  /** node_badge_v.badge — how many records have a story on this node. */
  count: number;
  /** The tier of the node's latest event. */
  tier: Tier | string;
  /** Records the system judged minor; counted, never hidden. */
  minor?: number;
  name?: string;
}

/**
 * The mark the Graph view puts on a node that has something to say. A node with
 * no records gets a hollow dot, not an absence — the difference between "nothing
 * happened" and "nobody wrote it down" is the whole product.
 */
export function NodeBadge({count, tier, minor = 0, name}: NodeBadgeProps) {
  const dot = (tokens.tiers as Record<string, {dot: string}>)[tier as string]?.dot ?? c["brand-muted"];
  const has = count > 0;
  return (
    <span data-badge={count} data-tier={tier} data-minor={minor}
          title={name ? `${name} · ${count} records with a story, ${minor} minor` : undefined}
          style={{display: "inline-flex", alignItems: "center", gap: 5, fontSize: tokens.type_scale.micro,
                  color: c["brand-gray"]}}>
      <span style={{width: 10, height: 10, borderRadius: tokens.radii.dot,
                    background: has ? tokens.colors["spark-wash"] : "transparent",
                    border: `2px solid ${dot}`}} />
      {has ? <b>{count}</b> : <span style={{color: c["brand-muted"]}}>0</span>}
      <span style={{color: c["brand-muted"]}}>条记录{minor > 0 ? ` · 另有 ${minor} 条次要` : ""}</span>
    </span>
  );
}

export default NodeBadge;
