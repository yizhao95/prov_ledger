import * as React from "react";
import tokens from "../tokens";

export type SourceLevel = "linked" | "verbal" | "task_context" | "unstated";

export interface SourceLevelBadgeProps {
  /** queries.SOURCE_LEVELS' key — the COMPUTED evidence level, never stored. */
  level: SourceLevel | string | null;
}

const WORDS: Record<string, {label: string; hint: string; color: string}> = {
  linked: {label: "有链接可查", hint: "a reference you can open", color: "brand-green"},
  verbal: {label: "有原话", hint: "the words themselves", color: "brand-blue"},
  task_context: {label: "只有任务脉络", hint: "inferred from the task around it", color: "brand-spark"},
  unstated: {label: "未说明", hint: "nothing was recorded", color: "brand-red"},
};

/**
 * How checkable a record's source is. Four levels, worst to best: nothing was
 * recorded, the task around it, the words themselves, a reference you can open.
 * Shown as words because "level 3" tells a reader nothing.
 */
export function SourceLevelBadge({level}: SourceLevelBadgeProps) {
  const key = (level && level in WORDS ? level : "unstated") as SourceLevel;
  const row = WORDS[key];
  const c = tokens.colors as Record<string, string>;
  return (
    <span data-source-level={key} title={`${key} — ${row.hint}`}
          style={{display: "inline-block", padding: "1px 6px", borderRadius: tokens.radii.chip,
                  fontSize: tokens.type_scale.micro, whiteSpace: "nowrap",
                  border: `1px solid ${c[row.color]}55`, color: c[row.color]}}>
      {row.label}
    </span>
  );
}

export default SourceLevelBadge;
