/* test.mjs — what a built library must be true of (DP phase 2d, Task 2).
 *
 * No vitest: the claims are structural (the bundle exports every component, a
 * preview card exists per component and is tagged for Claude Design, the
 * stylesheet's import closure reaches the bundle CSS), and a test runner would
 * be a dependency bought for nothing.
 */
import {readFileSync, readdirSync, existsSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "dist");
const previews = join(here, "previews");
let failures = 0;

function ok(name, cond, detail = "") {
  if (cond) { console.log(`  ok   ${name}`); return; }
  failures++;
  console.error(`  FAIL ${name}${detail ? " — " + detail : ""}`);
}

const COMPONENTS = ["TierBadge", "SourceLevelBadge", "HeadlineBlock", "TimelineEntry", "ViewSwitcher",
                    "NodeBadge", "SessionCard", "OutcomeRow", "ShownAdopted"];

console.log("bundle");
const bundlePath = join(dist, "_ds_bundle.js");
ok("dist/_ds_bundle.js exists", existsSync(bundlePath));
const bundle = existsSync(bundlePath) ? readFileSync(bundlePath, "utf8") : "";
for (const name of COMPONENTS) {
  ok(`exports ${name}`, new RegExp(`\\b${name}\\b`).test(bundle));
}
ok("is an IIFE on window.ProvLedgerDS", /var ProvLedgerDS\s*=/.test(bundle) || /ProvLedgerDS\s*=/.test(bundle));
ok("bundles React (no bare import left)", !/^\s*import\s+.*from\s+["']react["']/m.test(bundle));

console.log("styles");
const cssPath = join(dist, "_ds_bundle.css");
ok("dist/_ds_bundle.css exists", existsSync(cssPath));
const stylesPath = join(dist, "styles.css");
ok("dist/styles.css exists", existsSync(stylesPath));
if (existsSync(stylesPath)) {
  ok("styles.css imports the bundle css", readFileSync(stylesPath, "utf8").includes("_ds_bundle.css"));
}
if (existsSync(cssPath)) {
  const css = readFileSync(cssPath, "utf8");
  const tokens = JSON.parse(readFileSync(join(here, "../app/static/tokens.json"), "utf8"));
  ok("bundle css declares every colour token",
     Object.entries(tokens.colors).every(([k, v]) => css.includes(`--pl-${k}: ${v}`)));
}

console.log("previews");
const cards = existsSync(previews) ? readdirSync(previews).filter((f) => f.endsWith(".html")) : [];
ok(`one preview per component (${cards.length}/${COMPONENTS.length})`, cards.length >= COMPONENTS.length);
for (const name of COMPONENTS) {
  const file = join(previews, `${name}.html`);
  ok(`previews/${name}.html exists`, existsSync(file));
  if (!existsSync(file)) continue;
  const text = readFileSync(file, "utf8");
  const first = text.split("\n")[0].trim();
  ok(`previews/${name}.html first line is an @dsCard tag`,
     /^<!--\s*@dsCard\s+group="[^"]+"\s*-->$/.test(first), first);
  ok(`previews/${name}.html renders from dist`, text.includes("_ds_bundle.js"));
  ok(`previews/${name}.html links the stylesheet`, text.includes("styles.css"));
  ok(`previews/${name}.html shows more than one variant`,
     (text.match(/data-variant=/g) || []).length >= 2);
}

console.log(failures === 0 ? "\nall good" : `\n${failures} failure(s)`);
process.exit(failures === 0 ? 0 : 1);
