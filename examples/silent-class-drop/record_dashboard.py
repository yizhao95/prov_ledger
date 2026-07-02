#!/usr/bin/env python3
"""Record docs/media/silent-class-drop-dashboard.gif — the demo as the DASHBOARD
sees it, live.

Storyboard (~22s): the plan appears and steps go green (the false success) →
the verify step flips FAILED, red, auto-expanded, with the contract-MISMATCH
reason → the 📊 Data panel is opened to show the drift → decision trail → the
deviation sub-steps run and complete → the plan closes COMPLETED with the
failure still visible, never hidden.

Needs: playwright (chromium installed) + ffmpeg on PATH. The dashboard is
started read-only against the demo's throwaway DB; run_demo.py runs alongside
with DEMO_PACE so the 2s poll catches each state.

Usage, from the repo root:
    python examples/silent-class-drop/record_dashboard.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PORT = 8322
OUT_GIF = REPO / "docs" / "media" / "silent-class-drop-dashboard.gif"


def main() -> int:
    from playwright.sync_api import sync_playwright

    # Fresh DB so the recording starts from "no plans yet".
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
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/")
            # presentation only: the footer prints this machine's DB path
            page.add_style_tag(content="footer { display: none !important; }")

            # Run the demo alongside; DEMO_PACE spreads the acts so the 2s
            # dashboard poll catches: green steps -> FAILED verify -> revise.
            demo = subprocess.Popen(
                [sys.executable, str(HERE / "run_demo.py")],
                env=dict(os.environ, DEMO_PACE="2.2"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            # act 1: plan appears, ingest+cluster go green (the false success),
            # then the verify step flips FAILED (~t+7s with DEMO_PACE=2.2).
            page.wait_for_timeout(8000)
            # act 2: hold the red moment — center the FAILED card (auto-expanded
            # by the template, so it survives every HTMX swap).
            page.evaluate("""
              document.querySelector('.ring-red-400')
                ?.scrollIntoView({behavior: 'smooth', block: 'center'});
            """)
            page.wait_for_timeout(3500)
            # act 3: open the Data + Revision panels and keep them open — each
            # 2s HTMX swap re-renders them closed, so re-open on an interval.
            page.evaluate("""
              window.__keepOpen = setInterval(() => {
                document.querySelectorAll('details.border-cyan-300, details.border-amber-300')
                  .forEach(d => d.open = true);
              }, 400);
              document.querySelector('details.border-cyan-300')
                ?.scrollIntoView({behavior: 'smooth', block: 'start'});
            """)
            page.wait_for_timeout(6000)   # act 4: sub-steps run and complete
            demo.wait(timeout=60)
            page.wait_for_timeout(4000)   # final poll -> plan COMPLETED
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            page.wait_for_timeout(3000)   # closing frame: header, 100%, failure kept
            video_path = page.video.path()
            ctx.close()
            browser.close()

        # webm -> optimized GIF: 2-pass palette (96 colors is plenty for a flat
        # UI), 8fps, 870px, bayer dither — keeps it well under the 4MB budget.
        palette = Path(tmp) / "palette.png"
        scale = "fps=8,scale=870:-1:flags=lanczos"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                        "-vf", f"{scale},palettegen=max_colors=96",
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
