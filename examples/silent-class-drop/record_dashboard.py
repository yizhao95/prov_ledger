#!/usr/bin/env python3
"""Record docs/media/silent-class-drop-dashboard.gif — the demo as the DASHBOARD
sees it, live.

Storyboard (~32s, lightly fast-forwarded): opens on the freshly-published plan
with every step PENDING → the steps run for real (explore, contract, ingest,
cluster; amber "Running now", green COMPLETED) → the verify step flips FAILED,
red, auto-expanded, with the contract-MISMATCH reason + its log → the 📊 Data
panel opens on the drift → decision trail → the deviation sub-steps recover →
the plan closes COMPLETED with the failure still visible, never hidden.

Needs: playwright (chromium installed) + ffmpeg on PATH. The dashboard is
started read-only against the demo's throwaway DB; run_demo.py runs alongside
with DEMO_PACE so the 2s poll catches each state.

Usage, from the repo root:
    python examples/silent-class-drop/record_dashboard.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PORT = 8322
OUT_GIF = REPO / "docs" / "media" / "silent-class-drop-dashboard.gif"
SPEEDUP = 1.6   # gentle fast-forward so the ~45s session reads as ~28s


def _wait_for_plan(db_path: Path, timeout: float = 30) -> None:
    """Block until the demo has published its plan (so the recording opens on
    the PENDING plan, never on the 'DB not found' error page)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if db_path.exists():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                n = conn.execute("SELECT COUNT(*) FROM Plans").fetchone()[0]
                conn.close()
                if n:
                    return
            except sqlite3.Error:
                pass
        time.sleep(0.2)
    raise TimeoutError("demo never published a plan")


def main() -> int:
    from playwright.sync_api import sync_playwright

    # Fresh DB so the recording starts from a clean, deterministic run.
    (HERE / "demo-orchestrator.db").unlink(missing_ok=True)

    env = dict(os.environ, ORCH_DB=str(HERE / "demo-orchestrator.db"))
    dash = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=REPO / "orchestrator-webapp", env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    demo = None
    tmp = tempfile.mkdtemp(prefix="dash-rec-")
    try:
        time.sleep(3)  # dashboard boot
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 860},
                record_video_dir=tmp,
                record_video_size={"width": 1280, "height": 860},
            )

            # Start the demo FIRST; open the page only once the plan row
            # exists — the video begins on the PENDING plan.
            demo = subprocess.Popen(
                [sys.executable, str(HERE / "run_demo.py")],
                env=dict(os.environ, DEMO_PACE="2.0"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _wait_for_plan(HERE / "demo-orchestrator.db")

            page = ctx.new_page()   # video starts here
            page.goto(f"http://127.0.0.1:{PORT}/")
            # presentation only: the footer prints this machine's DB path
            page.add_style_tag(content="footer { display: none !important; } "
                           "* { animation: none !important; }")  # pulse bloats the GIF

            # act 1: the freshly-published plan, 5 steps PENDING (hold), then
            # the steps run for real — explore, contract, ingest, cluster.
            page.wait_for_timeout(16000)
            # act 2: wait for the verify step to actually flip FAILED, then
            # center the red card (auto-expanded, survives every HTMX swap).
            # Instant scrolls: smooth scrolling bloats the GIF with full-frame
            # deltas.
            page.wait_for_selector('.ring-red-400', timeout=30000)
            page.evaluate("""
              document.querySelector('.ring-red-400')
                ?.scrollIntoView({behavior: 'instant', block: 'center'});
            """)
            page.wait_for_timeout(4000)
            # act 3: open the Data + Revision panels and keep them open — each
            # 2s HTMX swap re-renders them closed, so re-open on an interval.
            page.evaluate("""
              window.__keepOpen = setInterval(() => {
                document.querySelectorAll('details.border-cyan-300, details.border-amber-300')
                  .forEach(d => d.open = true);
              }, 400);
              document.querySelector('details.border-cyan-300')
                ?.scrollIntoView({behavior: 'instant', block: 'start'});
            """)
            page.wait_for_timeout(6000)   # act 4: sub-steps recover
            demo.wait(timeout=90)
            page.wait_for_timeout(4000)   # final poll -> plan COMPLETED
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            page.wait_for_timeout(3000)   # closing frame: header, 100%, failure kept
            video_path = page.video.path()
            ctx.close()
            browser.close()

        # webm -> optimized GIF: gentle fast-forward, 2-pass palette (64 colors
        # is plenty for a flat UI), 6fps, 830px, bayer dither — under 4MB.
        palette = Path(tmp) / "palette.png"
        scale = f"setpts=PTS/{SPEEDUP},fps=6,scale=830:-1:flags=lanczos"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                        "-vf", f"{scale},palettegen=max_colors=64",
                        str(palette)], check=True)
        OUT_GIF.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                        "-i", str(palette),
                        "-lavfi", f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5",
                        str(OUT_GIF)], check=True)
        print(f"wrote {OUT_GIF} ({OUT_GIF.stat().st_size // 1024} KB)")
        return 0
    finally:
        if demo and demo.poll() is None:
            demo.kill()
        dash.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
