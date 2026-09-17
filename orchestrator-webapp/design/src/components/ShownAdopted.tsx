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
  /** read_hit rows — surfaced. */
  shown: Record_[];
  /** influence rows — adopted by a plan. */
  adopted: Record_[];
}

const MOMENTS: Record<string, string> = {
  plan: "at planning", edit: "before edit", close: "at close", why: "on query",
};

/**
 * Surfaced vs adopted — two counts that are NEVER derived from each other
 * (I11). A record shown in a headline is a read_hit; only an id an agent
 * actually cited becomes an influence. Nothing here claims anyone READ
 * anything: the system can record that something was shown, not that it
 * was understood.
 */
export function ShownAdopted({shown, adopted}: ShownAdoptedProps) {
  return (
    <div data-panel="shown-adopted" style={{display: "grid", gap: tokens.spacing.row}}>
      <Disclose summary={`Surfaced ${shown.length}`} count={undefined}>
        {shown.length === 0 && <Meta>Never surfaced.</Meta>}
        {shown.map((r, i) => (
          <div key={`${r.reason_id}-${i}`} data-shown={r.reason_id} style={{marginBottom: 4}}>
            <Meta>{MOMENTS[r.moment ?? ""] ?? r.moment} · </Meta>
            <span style={{color: c["brand-gray"], fontSize: tokens.type_scale.meta}}>“{r.text}”</span>
          </div>
        ))}
      </Disclose>
      <Disclose summary={`Adopted ${adopted.length}`} open={adopted.length > 0}>
        {adopted.length === 0 && <Meta>No prior decisions relied on.</Meta>}
        {adopted.map((r, i) => (
          <div key={`${r.reason_id}-${i}`} data-adopted={r.reason_id} style={{marginBottom: 4}}>
            <span style={{color: c["brand-gray"], fontSize: tokens.type_scale.meta}}>“{r.text}”</span>{" "}
            <Meta title={`via ${r.via ?? "?"}`}>(cited by {r.by ?? "agent"})</Meta>
          </div>
        ))}
      </Disclose>
    </div>
  );
}

export default ShownAdopted;
