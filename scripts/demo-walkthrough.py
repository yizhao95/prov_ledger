#!/usr/bin/env python3
"""demo-walkthrough.py — record the provenance scenario as a walkthrough.

Follows the five screens `examples/phantom-uplift/demo-provenance.sh` builds,
in the order a person would actually read them:

  1. the agent's task, and the block that says what history changed about it
  2. the rule that stopped it — expanded in place
  3. the thing itself: when the column went, and the words behind it
  4. the person's task, with their sentence and the email that carried it
  5. the map, reduced — with the page saying out loud that it is reduced

Each step gets a PALE TOP STRIP, never a banner over the content: the point is
to show the real page, and a dark overlay hides exactly the thing being shown.

Writes docs/media/provenance-walkthrough.mp4 and .gif.

MANUAL ONLY — this is not wired into CI. It needs a browser, a running
dashboard and ffmpeg, and a recording that flakes in CI teaches nobody anything.

    bash examples/phantom-uplift/demo-provenance.sh          # build the data
    ORCH_DB=<the demo db> bash orchestrator-webapp/launch_dashboard.sh &
    python3 scripts/demo-walkthrough.py --base http://127.0.0.1:8765 \\
        --urls "$DEMO_HOME/urls.txt"
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MEDIA = REPO / "docs" / "media"

# The pale strip: readable, and it never covers the page it is describing.
STRIP = """
(() => {
  let el = document.getElementById('pl-strip');
  if (!el) {
    el = document.createElement('div');
    el.id = 'pl-strip';
    el.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
      'background:#fcfcfbEE', 'color:#52514e', 'border-bottom:1px solid #e1e0d9',
      'font:500 13px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif',
      'padding:8px 16px', 'text-align:center', 'backdrop-filter:blur(2px)',
    ].join(';');
    document.body.appendChild(el);
    document.body.style.paddingTop = '38px';
  }
  el.textContent = %s;
})();
"""

STEPS = [
    ("1", "Task B — the agent's plan. Findings show a prior decision on this column.", None),
    ("2", "The constraint that stopped it, expanded in place.",
     '[data-panel="changed-by-history"], [data-panel="headline"]'),
    ("3", "The node itself: when the column went, and the reason recorded at the time.",
     '[data-panel="trace-strip"]'),
    ("4", "Task A — the person's own words, and the email behind them.",
     '[data-panel="changed-by-history"]'),
    ("5", "Graph — reduced view, clustered by module.", '[data-mode-chips]'),
]


def urls_from(path: Path) -> list[str]:
    """The paths demo-provenance.sh printed, in the order it printed them."""
    found = re.findall(r"^\s+(/(?:plan|node|graph)/\S+)$", path.read_text(encoding="utf-8"), re.M)
    if len(found) < 4:
        sys.exit(f"demo-walkthrough: only {len(found)} URLs in {path} — run demo-provenance.sh first")
    # screens 1 and 2 are the same page: the block, then the rule expanded
    return [found[0], found[0], found[1], found[2], found[3]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--urls", required=True, help="the urls.txt demo-provenance.sh wrote")
    ap.add_argument("--hold", type=float, default=3.5, help="seconds per screen")
    ap.add_argument("--out", default=str(MEDIA / "provenance-walkthrough"))
    a = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("demo-walkthrough: playwright is not installed (pip install playwright && playwright install chromium)")

    paths = urls_from(Path(a.urls))
    MEDIA.mkdir(parents=True, exist_ok=True)
    out = Path(a.out)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 860},
                                  record_video_dir=str(out.parent), record_video_size={"width": 1280, "height": 860})
        page = ctx.new_page()
        for (n, caption, expand), path in zip(STEPS, paths):
            page.goto(a.base + path, wait_until="networkidle")
            page.evaluate(STRIP % _js_string(f"{n}/5  {caption}"))
            if expand:
                for sel in expand.split(", "):
                    el = page.query_selector(sel)
                    if el:
                        el.scroll_into_view_if_needed()
                        break
            page.wait_for_timeout(int(a.hold * 1000))
        video = page.video.path() if page.video else None
        ctx.close()
        browser.close()

    if not video:
        sys.exit("demo-walkthrough: playwright recorded no video")
    mp4 = out.with_suffix(".mp4")
    Path(video).replace(mp4)
    print(f"demo-walkthrough: wrote {mp4.relative_to(REPO)}")

    if shutil.which("ffmpeg"):
        gif = out.with_suffix(".gif")
        palette = out.parent / "_palette.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4),
                        "-vf", "fps=10,scale=1000:-1:flags=lanczos,palettegen", str(palette)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-i", str(palette),
                        "-lavfi", "fps=10,scale=1000:-1:flags=lanczos[x];[x][1:v]paletteuse", str(gif)], check=True)
        palette.unlink(missing_ok=True)
        print(f"demo-walkthrough: wrote {gif.relative_to(REPO)}")
    else:
        print("demo-walkthrough: no ffmpeg on PATH — mp4 only, no gif")
    return 0


def _js_string(text: str) -> str:
    import json
    return json.dumps(text, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
