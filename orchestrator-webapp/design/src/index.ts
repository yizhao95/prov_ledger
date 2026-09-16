/* provLedger Design — the dashboard's components, compiled.
 *
 * Every prop here is a field the read-only dashboard already returns
 * (orchestrator-webapp/app/queries.py), so a design made against these
 * components is a design against real data, not a mock. The bundle exposes
 * them on window.ProvLedgerDS for the preview cards and for Claude Design.
 */
export {default as tokens} from "./tokens";
export * from "./primitives";
export {TierBadge, type TierBadgeProps, type Tier} from "./components/TierBadge";
export {SourceLevelBadge, type SourceLevelBadgeProps, type SourceLevel} from "./components/SourceLevelBadge";
export {HeadlineBlock, type HeadlineBlockProps, type Finding, type HeadlineSummary} from "./components/HeadlineBlock";
export {TimelineEntry, type TimelineEntryProps} from "./components/TimelineEntry";
export {ViewSwitcher, type ViewSwitcherProps, type Triple} from "./components/ViewSwitcher";
export {NodeBadge, type NodeBadgeProps} from "./components/NodeBadge";
export {SessionCard, type SessionCardProps} from "./components/SessionCard";
export {OutcomeRow, type OutcomeRowProps} from "./components/OutcomeRow";
export {ShownAdopted, type ShownAdoptedProps} from "./components/ShownAdopted";

export const COMPONENTS = [
  "TierBadge", "SourceLevelBadge", "HeadlineBlock", "TimelineEntry", "ViewSwitcher",
  "NodeBadge", "SessionCard", "OutcomeRow", "ShownAdopted",
] as const;

/* The preview cards render with no second <script>: React and a one-line mount
 * ride along in the bundle, so a card is a single file Claude Design can open
 * offline. Underscored because they are plumbing, not part of the component API. */
import * as React from "react";
import {createRoot} from "react-dom/client";
export const __React = React;
export function __render(node: React.ReactNode, host: HTMLElement) {
  createRoot(host).render(node);
}
