import * as React from "react";
import tokens from "../tokens";

export type Tier = "observed" | "derived" | "asserted" | "stated" | "unstated";

export interface TierBadgeProps {
  /** How the system decided this row's provenance. Never chosen by an LLM. */
  tier: Tier | string | null;
}

const WASH: Record<string, [string, string]> = {
  observed: ["brand-green", "brand-green"],
  derived: ["brand-blue", "brand-blue"],
  asserted: ["brand-spark", "spark-ink"],
  stated: ["brand-gray", "brand-gray"],
  unstated: ["brand-red", "brand-red"],
};

/**
 * The tier of one record. The LABEL is the differentiator (E3-1): each tier
 * reads as its own word and the tint only reinforces it, so the badge still
 * works in greyscale and for a colour-blind reader. `unstated` is a real tier —
 * an explicit gap — and is shown, never blended away.
 */
export function TierBadge({tier}: TierBadgeProps) {
  const key = (tier && tier in tokens.tiers ? tier : "unstated") as Tier;
  const row = tokens.tiers[key];
  const [bg, fg] = WASH[key];
  const c = tokens.colors as Record<string, string>;
  return (
    <span data-tier={key} title={`tier ${key}`}
          style={{display: "inline-block", padding: "1px 6px", borderRadius: tokens.radii.chip,
                  fontSize: tokens.type_scale.micro, whiteSpace: "nowrap",
                  background: c[bg] + "1a", color: c[fg]}}>
      {row.label}
    </span>
  );
}

export default TierBadge;
